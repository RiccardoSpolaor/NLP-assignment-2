import torch
from transformers import AutoTokenizer, PreTrainedTokenizer 
from .token_importances_extractor import TokenImportancesExtractor
from .encoder_decoder import build_encoder_decoder
from typing import List, Optional, Union

class Model(torch.nn.Module):
    """The question answering model. 

    This model takes in input the question, the passage and, optionally, the history, and it generates the answer.
    If given, the history is a single string where the different turns are separated with the special separator token 
    '<sep>'. It is used by concatenating with the question, using the same special token '<sep>', and then this concatenated
    string is given in input to the model.

    This model consists of two modules.
    1. The first one is the tokens importances extractor. Given the question and the passage (and, optionally, the history)
       it produces the tokens importances: for each passage token, a score in [0,1] is produced, representing the importance
       of that passage token. Basically, each score represents the likelihood that the token is in the span containing the
       answer.
    2. The second module is the encoder-decoder, i.e. the seq2seq model. Given the question, the passage (and, optionally,
       the history) and the tokens importances, it generates the answer.

    The reason for structuring in this way the model is the following. If we use only the encoder-decoder for generating the
    answer, the model can have difficulties in finding the interesting and useful information in the passage, since it can 
    be very long. Therefore, adding a module which gives to each token an importance, it can help the encoder-decoder in the
    answer generation. Basically, the purpose is similar to have a module which extracts the span of interest from the passage
    and then another module for generating an answer out of the extracted span. But the approach is different: we give an 
    importance score to each passage token. 

    Going more in depth, the tokens importances extractor is a transformer-based encoder (e.g. bert) with a linear layer on 
    top.
    Basically, for each input token, a contextual embedding vector is produced using the encoder, and then a probability score
    is computed using the linear layer.

    Instead, the encoder-decoder is a classic transformer-based encoder-decoder modified to make it accept the tokens 
    importances as second input of the encoder. The importances are injected inside the model by combining them to the input 
    hidden states of every encoder block. More precisely, for each encoder block, the tokens importances scores are 
    transformed into vectors of the same dimensionality of the block inputs vectors using a linear layer: then, these tokens 
    importances vectors are simply added to the block inputs vectors. We have chosen to use a different linear layer for 
    every block of the encoder: therefore there are $n$ additional linear layers, where $n$ is the number of encoder blocks.

    Both the tokens importances extractor and the encoder-decoder are built from a pre-trained transformer-based architecture.
    In particular, two kinds of pre-trained models can be specified.
    - Bert-tiny: 'prajjwal1/bert-tiny'.
      The token importances extractor is built from the bert-tiny encoder; the encoder-decoder is built from the bert-tiny 
      encoder-decoder.
    - Distil roberta: 'distilroberta-base'.
      The token importances extractor is built from the distil roberta encoder; the encoder-decoder is built from the distil 
      roberta encoder-decoder. 

    For the implementation details of the two modules, see the python files `token_importances_extractor.py` and 
    `encoder_decoder.py`.

    Parameters
    ----------
    model_name : str
        Name of the pre-trained model to use, either 'prajjwal1/bert-tiny' or 'distilroberta-base'.
    tokenizer : Optional[PreTrainedTokenizer], optional
        Pre-trained tokenizer to use, by default None.
        If None, the default tokenizer of the pre-trained model is used.
    device : str, optional
        Device onto which attach the model, by default 'cuda'
   
    """
    def __init__(self, model_name : str, tokenizer: Optional[PreTrainedTokenizer] = None, device : str = 'cuda'):
        super().__init__()
        self.model_name = model_name 
        self.device = device

        # Tokens importances extractor
        self.token_importances_extractor = TokenImportancesExtractor(model_name)
        self.token_importances_extractor.to(device)

        # Encoder-decoder
        self.encoder_decoder = build_encoder_decoder(model_name=model_name)
        self.encoder_decoder.to(device)

        if tokenizer is None:
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        else:
            # Add information about special tokens to the Encoder-Decoder module if the tokenizer is provided.
            self.tokenizer = tokenizer
        self.encoder_decoder.config.decoder_start_token_id = self.tokenizer.cls_token_id
        self.encoder_decoder.config.pad_token_id = self.tokenizer.pad_token_id


    def generate(self, passage : List[str], question : List[str], history : Optional[List[str]] = None, 
                 generation_params : Optional[dict] = None, return_importances: bool = False) -> str:
        """Generate the answer, given the passage, the question and, optionally, the history.

        First of all, the tokens importances scores are produced by the extractor module, then the answer is generated by the
        encoder-decoder using also the tokens importances scores.

        It is very important to point out that this method works on a batch. So, a batch of passages, questions and, optionally,
        histories is given, producing a batch of answers.

        Parameters
        ----------
        passage : List[str]
            Batch of passages
        question : List[str]
            Batch of questions
        history : Optional[List[str]], optional
            Batch of histories, by default None.
            Each history is a string in which the special separator token '<sep>' is used for separating the different turns.
        generation_params : Optional[dict], optional
            Parameters for the generation, by default None
        return_importances : bool, optional
            Whether to return also the token importances scores or not, by default False

        Returns
        -------
        generated_text : List[str]
            Batch of generated answers
        token_importances_output : Tensor
            Batch of tokens importances scores

        """
        # Set generation parameters.
        if generation_params is None:
            # Default genration parameters
            generation_params = { 'do_sample': False, 'num_beams': 3, 'repetition_penalty': 2. }
        self.generation_params = generation_params

        # If given, inject the history into question
        if history is not None:
            history = tuple([h.split(' <sep> ') for h in history])
            separator = f' {self.tokenizer.sep_token} '
            question_and_history = tuple([q + f'{separator if len(h) else ""}' + separator.join(h) for q, h in zip(question, history)])
        else:
            question_and_history = question

        # Tokenized model inputs
        inputs = self.tokenizer(
                question_and_history,
                passage,
                max_length=512,
                truncation=True,
                padding=True,
                return_tensors="pt",
            ).to(self.device)

        # Forward pass
        with torch.no_grad():
            # Tokens importances extractor forward pass
            token_importances_output = self.token_importances_extractor.forward(inputs.input_ids, inputs.attention_mask)
            # Encoder-decoder forward pass
            generated_ids = self.encoder_decoder.generate(inputs.input_ids, token_importances=token_importances_output, 
                                                          **generation_params)
            # Generated answers
            generated_text = self.tokenizer.batch_decode(generated_ids, skip_special_tokens=True)

        if return_importances:
            return generated_text, token_importances_output
        else:
            return generated_text


    def compute_token_importances(self, passage, question, history=None):
        """Compute the tokens importances scores, given the passage, the question and, optionally, the history.

        It is important to point out that this method works on a single sample, not a batch.

        Parameters
        ----------
        passage : str
            Passage
        question : str
            Question
        history : str, optional
            History, by default None.
            It is a string in which the special separator token '<sep>' is used for separating the different turns. 

        Returns
        -------
        Tensor
            Tokens importances scores
        """
        if history is not None:
            history = history.split(' <sep> ')
            separator = f' {self.tokenizer.sep_token} '
            question_and_history = question + f'{separator if len(history) else ""}' + separator.join(history)
        else:
            question_and_history = question

        inputs = self.tokenizer(
                question_and_history,
                passage,
                max_length=512,
                truncation=True,
                padding=True,
                return_tensors="pt",
            ).to(self.device)

        with torch.no_grad():
            token_importances_output = self.token_importances_extractor.forward(inputs.input_ids, inputs.attention_mask)

        return token_importances_output



    def load_weigths(self, tokenImportancesExtractor_weigths_path : str, encoderDecoder_weigths_path : str):
        self.token_importances_extractor.load_state_dict(torch.load(tokenImportancesExtractor_weigths_path)) 
        self.encoder_decoder.load_state_dict(torch.load(encoderDecoder_weigths_path)) 