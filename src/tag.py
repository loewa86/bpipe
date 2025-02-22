import json
import pandas as pd
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModelForSequenceClassification, pipeline
from finvader import finvader
from huggingface_hub import hf_hub_download
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import tensorflow as tf
import swifter, os
from exorde_data import (
    Translation,
    LanguageScore,
    Sentiment,
    Embedding,
    SourceType,
    TextType,
    Emotion,
    Irony,
    Age,
    Gender,
    Analysis,
)

from opentelemetry import trace
from opentelemetry.trace import StatusCode

# sentence_transformers
# transformers
# finvader
# huggingface
# vader

class TokenAndPositionEmbedding(tf.keras.layers.Layer):
    def __init__(self, maxlen, vocab_size, embed_dim, **__kwargs__):
        super().__init__()
        self.token_emb = tf.keras.layers.Embedding(
            input_dim=vocab_size, output_dim=embed_dim
        )
        self.pos_emb = tf.keras.layers.Embedding(
            input_dim=maxlen, output_dim=embed_dim
        )

    def call(self, x):
        maxlen = tf.shape(x)[-1]
        positions = tf.range(start=0, limit=maxlen, delta=1)
        positions = self.pos_emb(positions)
        x = self.token_emb(x)
        return x + positions


class TransformerBlock(tf.keras.layers.Layer):
    def __init__(self, embed_dim, num_heads, ff_dim, rate=0.1, **__kwargs__):
        super().__init__()
        self.att = tf.keras.layers.MultiHeadAttention(
            num_heads=num_heads, key_dim=embed_dim
        )
        self.ffn = tf.keras.Sequential(
            [
                tf.keras.layers.Dense(ff_dim, activation="relu"),
                tf.keras.layers.Dense(embed_dim),
            ]
        )
        self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.dropout1 = tf.keras.layers.Dropout(rate)
        self.dropout2 = tf.keras.layers.Dropout(rate)

    def call(self, inputs, training):
        attn_output = self.att(inputs, inputs)
        attn_output = self.dropout1(attn_output, training=training)
        out1 = self.layernorm1(inputs + attn_output)
        ffn_output = self.ffn(out1)
        ffn_output = self.dropout2(ffn_output, training=training)
        return self.layernorm2(out1 + ffn_output)


def tag(documents: list[str], lab_configuration):
    """
    Analyzes and tags a list of text documents using various NLP models and techniques.

    The function processes the input documents using pre-trained models for tasks such as
    sentence embeddings, text classification, sentiment analysis, and custom models for age,
    gender, and hate speech detection. It returns a list of dictionaries containing the
    processed data for each input document.

    Args:
        documents (list): A list of text documents (strings) to be analyzed and tagged.
        nlp: model
        device: device
        mappings: labels

    Returns:
        list: A list of dictionaries, where each dictionary represents a single input text and
              contains various processed data like embeddings, text classifications, sentiment, etc.,
              as key-value pairs.
    """
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("tag_init") as tag_init_span:
        nlp = lab_configuration["nlp"]
        device = lab_configuration["device"]
        mappings = lab_configuration["mappings"]

        def predict(text, pipe, tag, mappings):
            preds = pipe.predict(text, verbose=0)[0]
            result = []
            for i in range(len(preds)):
                result.append((mappings[tag][i], float(preds[i])))
            return result

        # get text content attribute from all items
        for doc in documents:
            assert isinstance(doc, str)

        # Create an empty DataFrame
        tmp = pd.DataFrame()

        # Add the original text documents
        tmp["Translation"] = documents

        assert tmp["Translation"] is not None
        assert len(tmp["Translation"]) > 0

        tag_init_span.set_status(StatusCode.OK)


    with tracer.start_as_current_span("tag_sentence_embeddings") as sentence_embedding_span:
        # Compute sentence embeddings
        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        tmp["Embedding"] = tmp["Translation"].swifter.apply(
            lambda x: list(model.encode(x).astype(float))
        )
        sentence_embedding_span.set_status(StatusCode.OK)

    # Text classification pipelines
    text_classification_models = [
        ("Emotion", "SamLowe/roberta-base-go_emotions"),
        # ("Irony", "cardiffnlp/twitter-roberta-base-irony"),
        ("LanguageScore", "salesken/query_wellformedness_score"),
        # ("TextType", "marieke93/MiniLM-evidence-types"),
    ]


    for col_name, model_name in text_classification_models:
        with tracer.start_as_current_span(model_name) as model_span:
            pipe = pipeline(
                "text-classification",
                model=model_name,
                top_k=None,
                device=device,
                max_length=512,
                padding=True,
            )
            tmp[col_name] = tmp["Translation"].swifter.apply(
                lambda x: [(y["label"], float(y["score"])) for y in pipe(x)[0]]
            )
            del pipe  # free ram for latest pipe
            model_span.set_status(StatusCode.OK)

    with tracer.start_as_current_span('tokenization') as tokenization_span:
        # Tokenization for custom models
        tokenizer = AutoTokenizer.from_pretrained("bert-large-uncased")
        tmp["Embedded"] = tmp["Translation"].swifter.apply(
            lambda x: np.array(
                tokenizer.encode_plus(
                    x,
                    add_special_tokens=True,
                    max_length=512,
                    truncation=True,
                    padding="max_length",
                    return_attention_mask=False,
                    return_tensors="tf",
                )["input_ids"][0]
            ).reshape(1, -1)
        )
        tokenization_span.set_status(StatusCode.OK)


    def load_cached_file(filepath):
        with open(filepath, 'r') as file:
            return json.load(file)

    def get_cached_file_path(filename):
        # Update the base cache directory to include the nested directories
        base_cache_dir = os.path.join(os.getenv('HOME'), '.cache', 'huggingface', 'hub', 'models--ExordeLabs--SentimentDetection', 'snapshots', '0eac9e0d21db6f342d5492d5db727fb00c767c40')
        filepath = os.path.join(base_cache_dir, filename)
        if os.path.exists(filepath):
            return filepath
        else:
            raise FileNotFoundError(f"{filename} not found in cache directory.")

    with tracer.start_as_current_span('sentiment_detection') as sentiment_span:
        emoji_lexicon_path = get_cached_file_path('emoji_unic_lexicon.json')
        loughran_dict_path = get_cached_file_path('loughran_dict.json')

        # Corrected: Load the lexicons directly without unnecessary open statements
        emoji_lexicon = load_cached_file(emoji_lexicon_path)
        loughran_dict = load_cached_file(loughran_dict_path)

        sentiment_analyzer = SentimentIntensityAnalyzer()
        sentiment_analyzer.lexicon.update(loughran_dict)
        sentiment_analyzer.lexicon.update(emoji_lexicon)

        # Assuming setting status on sentiment_span is part of your tracing system
        sentiment_span.set_status(StatusCode.OK)  # Adjust according to your tracing system's API


    with tracer.start_as_current_span('roberta_init') as roberta_init_span:
        ############################
        # financial distilroberta
        fdb_tokenizer = AutoTokenizer.from_pretrained("mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis")
        fdb_model = AutoModelForSequenceClassification.from_pretrained("mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis")
        roberta_init_span.set_status(StatusCode.OK)

    with tracer.start_as_current_span('distilbert_init') as distilbert_init_span:
        ############################
        # distilbert sentiment
        gdb_tokenizer = AutoTokenizer.from_pretrained("lxyuan/distilbert-base-multilingual-cased-sentiments-student")
        gdb_model = AutoModelForSequenceClassification.from_pretrained("lxyuan/distilbert-base-multilingual-cased-sentiments-student")
        ############################
        distilbert_init_span.set_status(StatusCode.OK)

    fdb_pipe = pipeline(
        "text-classification",
        model=fdb_model,
        tokenizer=fdb_tokenizer,
        top_k=None, 
        max_length=512,
        padding=True,
    )

    gdb_pipe = pipeline(
        "text-classification",
        model=gdb_model,
        tokenizer=gdb_tokenizer,
        top_k=None, 
        max_length=512,
        padding=True,
    )

    def vader_sentiment(text):
        # predict financial sentiment 
        return round(sentiment_analyzer.polarity_scores(text)["compound"],2)
    
    def fin_vader_sentiment(text):
        # predict general sentiment 
        return round(finvader(text, 
                        use_sentibignomics = True, 
                        use_henry = True, 
                        indicator = 'compound' ),2)

    def fdb_sentiment(text):
        prediction = fdb_pipe(text)
        fdb_sentiment_dict = {}
        for e in prediction[0]:
            if e["label"] == "negative":
                fdb_sentiment_dict["negative"] = round(e["score"],3)
            elif e["label"] == "neutral":
                fdb_sentiment_dict["neutral"] =  round(e["score"],3)
            elif e["label"] == "positive":
                fdb_sentiment_dict["positive"] =  round(e["score"],3)
        # compounded score
        fdb_compounded_score = round((fdb_sentiment_dict["positive"] - fdb_sentiment_dict["negative"]),3)
        return fdb_compounded_score

    def gdb_sentiment(text):
        # predict general sentiment 
        prediction = gdb_pipe(text)
        gen_distilbert_sent = {}
        for e in prediction[0]:
            if e["label"] == "negative":
                gen_distilbert_sent["negative"] = round(e["score"],3)
            elif e["label"] == "neutral":
                gen_distilbert_sent["neutral"] =  round(e["score"],3)
            elif e["label"] == "positive":
                gen_distilbert_sent["positive"] =  round(e["score"],3)
        # compounded score
        gdb_score = round((gen_distilbert_sent["positive"] - gen_distilbert_sent["negative"]),3)
        return gdb_score
    
    def compounded_financial_sentiment(text):
        #  65% financial distil roberta model + 35% fin_vader_score
        fin_vader_sent = fin_vader_sentiment(text)
        fin_distil_score = fdb_sentiment(text)
        fin_compounded_score = round((0.70 * fin_distil_score + 0.30 * fin_vader_sent),2)
        return fin_compounded_score
        
    def compounded_sentiment(text):
        # compounded_total_score: gen_distilbert_sentiment * 60% + vader_sentiment * 20% + compounded_fin_sentiment * 20%
        gen_distilbert_sentiment = gdb_sentiment(text)
        vader_sent = vader_sentiment(text)
        compounded_fin_sentiment = compounded_financial_sentiment(text)
        if abs(compounded_fin_sentiment) >= 0.6:
            compounded_total_score = round((0.30 * gen_distilbert_sentiment + 0.10 * vader_sent + 0.60 * compounded_fin_sentiment),2)
        elif abs(compounded_fin_sentiment) >= 0.4:
            compounded_total_score = round((0.40 * gen_distilbert_sentiment + 0.20 * vader_sent + 0.40 * compounded_fin_sentiment),2)
        elif abs(compounded_fin_sentiment) >= 0.1:
            compounded_total_score = round((0.60 * gen_distilbert_sentiment + 0.25 * vader_sent + 0.15 * compounded_fin_sentiment),2)
        else:  # if abs(compounded_fin_sentiment) < 0.1, so no apparent financial component
            compounded_total_score = round((0.60 * gen_distilbert_sentiment + 0.40 * vader_sent),2)
        return compounded_total_score

    with tracer.start_as_current_span('sentiment_analysis') as sentiment_span:
        # sentiment swifter apply compounded_sentiment
        tmp["Sentiment"] = tmp["Translation"].swifter.apply(compounded_sentiment)
        
        # financial sentiment swifter apply compounded_financial_sentiment
        tmp["FinancialSentiment"] = tmp["Translation"].swifter.apply(compounded_financial_sentiment)
        sentiment_span.set_status(StatusCode.OK)

    # Custom model pipelines
    custom_model_data = [
        # ("Age", "ExordeLabs/AgeDetection", "ageDetection.h5"),
        # ("Gender", "ExordeLabs/GenderDetection", "genderDetection.h5"),
        # (
        #     "HateSpeech",
        #     "ExordeLabs/HateSpeechDetection",
        #     "hateSpeechDetection.h5",
        # ),
    ]

    for col_name, repo_id, file_name in custom_model_data:
        with tracer.start_as_current_span(f'{file_name}') as model_loading_span:
            model_file = hf_hub_download(repo_id=repo_id, filename=file_name)
            custom_model = tf.keras.models.load_model(
                model_file,
                custom_objects={
                    "TokenAndPositionEmbedding": TokenAndPositionEmbedding,
                    "TransformerBlock": TransformerBlock,
                },
            )
            tmp[col_name] = tmp["Embedded"].swifter.apply(
                lambda x: predict(x, custom_model, col_name, mappings)
            )
            del custom_model  # free ram for latest custom_model
            model_loading_span.set_status(StatusCode.OK)

    del tmp["Embedded"]
    # The output is a list of dictionaries, where each dictionary represents a single input text and contains
    # various processed data like embeddings, text classifications, sentiment, etc., as key-value pairs.
    # Update the items with processed data


    tmp = tmp.to_dict(orient="records")

    _out = []
    for i in range(len(tmp)):
        language_score = LanguageScore(tmp[i]["LanguageScore"][0][1])

        sentiment = Sentiment(tmp[i]["Sentiment"])

        embedding = Embedding(tmp[i]["Embedding"])

        #gender = Gender(
        #    male=tmp[i]["Gender"][0][1], female=tmp[i]["Gender"][1][1]
        #)

        # types = {item[0]: item[1] for item in tmp[i]["TextType"]}
        # text_type = TextType(
        #     assumption=types["Assumption"],
        #     anecdote=types["Anecdote"],
        #     none=types["None"],
        #     definition=types["Definition"],
        #     testimony=types["Testimony"],
        #     other=types["Other"],
        #     study=types["Statistics/Study"],
        # )

        emotions = {item[0]: item[1] for item in tmp[i]["Emotion"]}
        emotion = Emotion(
            love=emotions["love"],
            admiration=emotions["admiration"],
            joy=emotions["joy"],
            approval=emotions["approval"],
            caring=emotions["caring"],
            excitement=emotions["excitement"],
            gratitude=emotions["gratitude"],
            desire=emotions["desire"],
            anger=emotions["anger"],
            optimism=emotions["optimism"],
            disapproval=emotions["disapproval"],
            grief=emotions["grief"],
            annoyance=emotions["annoyance"],
            pride=emotions["pride"],
            curiosity=emotions["curiosity"],
            neutral=emotions["neutral"],
            disgust=emotions["disgust"],
            disappointment=emotions["disappointment"],
            realization=emotions["realization"],
            fear=emotions["fear"],
            relief=emotions["relief"],
            confusion=emotions["confusion"],
            remorse=emotions["remorse"],
            embarrassment=emotions["embarrassment"],
            surprise=emotions["surprise"],
            sadness=emotions["sadness"],
            nervousness=emotions["nervousness"],
        )

        # ironies = {item[0]: item[1] for item in tmp[i]["Irony"]}

        # irony = Irony(irony=ironies["irony"], non_irony=ironies["non_irony"])

        # ages = {item[0]: item[1] for item in tmp[i]["Age"]}

        #age = Age(
        #    below_twenty=ages["<20"],
        #    twenty_thirty=ages["20<30"],
        #    thirty_forty=ages["30<40"],
        #    forty_more=ages[">=40"],
        #)

        analysis = Analysis(
            language_score=language_score,
            sentiment=sentiment,
            embedding=embedding,
            #gender=gender,
            # text_type=text_type,
            emotion=emotion,
            # irony=irony,
            #age=age,
        )

        _out.append(analysis)
    return _out
