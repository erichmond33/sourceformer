import json
from typing import List, Tuple, Union
import torch
from transformers import (
    PreTrainedTokenizerBase,
    pipeline,
    PreTrainedModel,
    TextGenerationPipeline,
)
from einops import rearrange, reduce

from torch import nn

MAX_BATCH_SIZE = 1  # My 3090 is weak 😔
N = 64  # SEQ Len
M = 16  # Min Loss Span To Consider

def log(t, eps = 1e-20):
    return t.clamp(min = eps).log()

class APICallPostprocessing:
    def __init__(
        self,
        start_tokens: List[int],
        end_tokens: List[int],
        minimum_percentage: float = 0.1,
        api_text: str = "",
        k_values: int = 20,
        m_generations = 10,
    ):
        """
        Base API Postprocesing class

        :param start_tokens: token representation for [ or other tokens
        :param end_tokens:  token representation for ] or other tokens
        :param minimum_percentage: pass percentage for candidate generation, less than this are ignored.
        """
        self.start_tokens = start_tokens
        self.end_tokens = end_tokens
        self.minimum_percentage = minimum_percentage
        self.api_text = api_text
        self.k_values = k_values
        self.m_generations = m_generations

    def filter_continuations(
        self,
        input_tokens: torch.Tensor,
        input_logits: torch.Tensor,
        labels: torch.Tensor,
        input_start: int,
        tokenizer: PreTrainedTokenizerBase,
    ) -> (torch.Tensor, torch.Tensor):
        """
        Grab continuations that are valid

        :param input_tokens: tokenized inputs
        :param input_logits: input logits
        :param labels: labels for input logits
        :param input_start: start of real input
        :param tokenizer:
        :return: Values (1, k) and Indices (1, k)
        """
        # Find the token for the word "is"
        is_token = tokenizer.convert_tokens_to_ids(["is"])[0]
        iis_token = tokenizer.convert_tokens_to_ids([" is"])[0]

        # Convert input_tokens to string
        input_tokensss = tokenizer.convert_ids_to_tokens(input_tokens[0].tolist())

        # First, figure out locations...
        # Calculate next token probabilites at every word in the sentence
        probs = torch.softmax(input_logits, dim=-1)
        # Make sure we don't keep any tokens that are supposed to be [
        remove_tokens = 1.0 - torch.sum(
            torch.stack([labels == start_token for start_token in self.start_tokens]),
            dim=0,
        )
        # Get maximum probability... Should be sufficient. Maybe switch to sum if there's issues later
        # Given two sets of probabilites for each start token, grab the hightest probability of the two at each word in the sentence.
        max_start_tokens = torch.amax(
            # Grab all the start_tokens & put them in a tensor
            # shape: (2, 1, 128) | 2 start tokens, 1 sentence, 128 <API> token call probabilities
            torch.stack(
                [probs[:, :, start_token] for start_token in self.start_tokens]
            ),
            dim=0,
        )
        # Exclude the probabilites of actual [ token predictions that exsist in the sentence.
        # (Rememeber <API> is just the "[" or " [" token)
        max_start_tokens = max_start_tokens * remove_tokens
        values, indices = torch.topk(max_start_tokens[:, : -(M + 1)], k=self.k_values, dim=1)

        # Convert indices to their string values
        indices2 = indices + input_start
        indices2 = indices.tolist()

        return values, indices

    def create_candidates(
        self,
        indices: torch.Tensor,
        values: torch.Tensor,
        input_tokens: torch.Tensor,
        labels: torch.Tensor,
        input_start: int,
        model: PreTrainedModel,
        tokenizer: PreTrainedTokenizerBase,
        generator: TextGenerationPipeline,
        criterion: nn.CrossEntropyLoss,
        device
    ):
        """
        Generates continuations of valid API calls

        :param indices: index to start
        :param values: values for filtering
        :param input_tokens: tokenized input
        :param labels: labels for input
        :param input_start: real start for base loss calculation
        :param model:
        :param tokenizer:
        :param generator: pipeline for text generation
        :param criterion: Should just be CE loss
        :return:
        """
        # Setup lists...
        outputs = list()
        num_to_keeps = list()
        texts_to_test = list()
        max_index = 0
        # loop through topK <API> calls
        for i, batch in enumerate(indices):
            for j, index in enumerate(batch):
                # Ensure probability meets the τs
                if values[i][j] < self.minimum_percentage:
                    continue
                # Get base output
                base_outputs = model(input_tokens[:, input_start:].to(device)).logits[
                    # :, index : index + M
                    :, index : 
                ]
                # Find starting location...
                num_keep = int(input_tokens[:, input_start:].shape[1] - index)
                # Calculate loss without API
                base_loss = criterion(
                    base_outputs.view(-1, base_outputs.size(-1)),
                    # labels[:, index : index + M].to(device).view(-1),
                    labels[:, index : ].to(device).view(-1),
                )
                # For padding later
                max_index = max(max_index, index)
                # API Text
                texts_to_test.append(
                    tokenizer.decode(input_tokens[:, : input_start + index][i])
                    + f" [{self.api_text}"
                )
                # grab m_generations
                outputs.append(
                    generator(
                        texts_to_test[-1], max_new_tokens=28, num_return_sequences=self.m_generations
                    )
                )
                # Add additional items to generation outputs...
                for k in range(self.m_generations):
                    outputs[-1][k]["index"] = int(index)
                    outputs[-1][k]["base_loss"] = float(base_loss.item())
                    outputs[-1][k]["base_outputs"] = base_outputs
                # So we know where to look
                num_to_keeps.append(num_keep)
        return outputs, num_to_keeps, texts_to_test, max_index

    def add_api_calls(
        self,
        candidate: int,
        outputs: dict,
        texts_to_test: List[str],
        tokenizer: PreTrainedTokenizerBase,
        input_tokens: torch.Tensor,
        input_start: int,
        nums_to_keep: List[int],
        base_loss: float,
        *args,
        **kwargs,
    ):
        """
        Add API calls here.

        :param candidate: which candidate is being parsed
        :param outputs: individual candidate outputs
        :param texts_to_test: text for candidates
        :param tokenizer:
        :param input_tokens:
        :param input_start:
        :param nums_to_keep: values kept after generation
        :param base_loss: base loss value for candidate
        :param args: args to pass to subclass
        :param kwargs: kwargs to pass to subclass
        :return:
        """
        raise NotImplementedError("Fill this in with your API code please!")

    def get_logits(
        self,
        model,
        generated_texts,
        max_token_len,
        max_token_len_base,
    ):
        # shape the batches...
        for j in range(len(generated_texts)):
            # Adding zeros to ensure each example of test_outputs & base_outputs has a length of max_token_len
            # (So we can cat all of them together)
            generated_texts[j].append(
                max_token_len - generated_texts[j][0].shape[1]
            )
            if generated_texts[j][-1] != 0:
                generated_texts[j][0] = torch.cat(
                    (
                        generated_texts[j][0],
                        torch.zeros(
                            (1, generated_texts[j][-1]),
                            dtype=generated_texts[j][0].dtype,
                            device=generated_texts[j][0].device,
                        ),
                    ),
                    dim=1,
                )
            generated_texts[j].append(
                max_token_len_base - generated_texts[j][1].shape[1]
            )
            if generated_texts[j][-1] != 0:
                generated_texts[j][1] = torch.cat(
                    (
                        generated_texts[j][1],
                        torch.zeros(
                            (1, generated_texts[j][-1]),
                            dtype=generated_texts[j][1].dtype,
                            device=generated_texts[j][1].device,
                        ),
                    ),
                    dim=1,
                )
            
        # Putting the the generated_texts into a single tensor and running a forward pass
        test_outputs = model(
            torch.cat(
                list(generated_text[0] for generated_text in generated_texts),
                dim=0,
            )
        ).logits
        base_outputs = model(
            torch.cat(
                list(generated_text[1] for generated_text in generated_texts),
                dim=0,
            )
        ).logits

        return generated_texts, test_outputs, base_outputs


    def filter_api( 
        self,
        model,
        outputs: List,
        max_token_len,
        max_token_len_base,
    ):
        def _compute_weight(t: int) -> Union[int, float]:
            """Compute the weight in the loss function."""
            return max(0, 1-0.2*t)
        
        for generated_texts in outputs:
            # These are the weights described by the weighting function in appendix A
            weights = torch.tensor([[1/3, .8/3, .6/3, .4/3, .2/3]],dtype=torch.float32).to(device=0)

            for j in range(len(generated_texts)):
                # Generate the weights tensor
                # (len(generated_texts), 0:-num_to_keeps)
                # 0:-num_to_keeps -> [1/3, .8/3, .6/3, .4/3, .2/3, 0, 0, ..., 0]
                

                num_to_keep = generated_texts[j][2]
                # Padding the weight matrix
                zeros_padding = torch.zeros(
                                    (1, num_to_keep - weights.shape[0]),
                                    dtype=generated_texts[j][1].dtype,
                                    device=generated_texts[j][1].device,
                                )
                weights_and_zeros_padding = torch.cat((weights, zeros_padding), dim=1)
                # Duplicating the weight matrix for each generation at position i
                weights_and_zeros_padding = weights_and_zeros_padding.repeat(len(generated_texts), 1)

                



                # Generate the losses

                # Multiply the two

        
        

    def generate_continuations(
        self,
        input_tokens: torch.Tensor,
        input_logits: torch.Tensor,
        labels: torch.Tensor,
        model: PreTrainedModel,
        tokenizer: PreTrainedTokenizerBase,
        device,
        *args,
        **kwargs,
    ):
        """
        Generate continuations

        :param input_tokens: input to model
        :param input_logits: output from model
        :param labels: labels for logits
        :param model:
        :param tokenizer:
        :param args: args to pass to add_api_calls
        :param kwargs: kwargs to pass to add_api_calls
        :return: individual candidate outputs
        """
        # Setup token stuff...
        ## The start_str is the prompt without our addition
        input_start = input_tokens.shape[1] - input_logits.shape[1]
        start_str = tokenizer.decode(input_tokens[:, :input_start][0])
        # Find top tokens...
        values, indices = self.filter_continuations(
            input_tokens, input_logits, labels, input_start, tokenizer
        )
        print(f"Found k_values: {values.shape[1]}")
        # setup generation calls...
        generator = pipeline(
            "text-generation", model=model, tokenizer=tokenizer, device=device
        )  # type: TextGenerationPipeline
        criterion = nn.CrossEntropyLoss()
        with torch.no_grad():
            outputs, num_to_keeps, texts_to_test, max_index = self.create_candidates(
                indices,
                values,
                input_tokens,
                labels,
                input_start,
                model,
                tokenizer,
                generator,
                criterion,
                device
            )
            for i in range(len(outputs)):
                generated_texts, max_token_len, max_token_len_base = self.add_api_calls(
                    i,
                    outputs[i],
                    texts_to_test,
                    tokenizer,
                    input_tokens,
                    input_start,
                    num_to_keeps,
                    outputs[i][0]["base_loss"],
                    device,
                    *args,
                    **kwargs,
                )
                if len(generated_texts) == 0:
                    outputs[i] = None
                    continue

                # These are the weights described by the weighting function in appendix A
                weights = torch.tensor([[1/3, .8/3, .6/3, .4/3, .2/3]],dtype=torch.float32).to(device=device)#args.device_id)
                # A varible to track the best loss in generated_texts
                best_loss = -99.0
                for j in range(len(generated_texts)):
                    tokens, tokens_with_api_response, tokens_without_api_response = generated_texts[j][-1] ,generated_texts[j][0], generated_texts[j][1]
                    
                    model.eval()
                    logits, logits_with_api_response, logits_without_api_response = map(model, (tokens, tokens_with_api_response, tokens_without_api_response))

                    def get_pred_prob(token_ids, logits):
                        logits = logits.logits[:, :-1]             # logits of each token...    (omit last logit)
                        token_ids = token_ids[:, 1:]        # predicts the next token id (omit first token id)

                        token_ids = rearrange(token_ids, 'b n -> b n 1')
                        probs = logits.softmax(dim = -1)
                        correct_token_id_pred_prob = probs.gather(-1, token_ids)
                        return rearrange(correct_token_id_pred_prob, 'b n 1 -> b n')
                    
                    probs                       = get_pred_prob(tokens, logits)
                    probs_without_api_response  = get_pred_prob(tokens_without_api_response, logits_without_api_response)
                    probs_with_api_response     = get_pred_prob(tokens_with_api_response, logits_with_api_response)

                    num_to_keep = generated_texts[j][2]
                    # Padding the weight matrix
                    zeros_padding = torch.zeros(
                                        (1, num_to_keep - weights.shape[1]),
                                        dtype=generated_texts[j][1].dtype,
                                        device=generated_texts[j][1].device,
                                    )
                    weights_and_zeros_padding = torch.cat((weights, zeros_padding), dim=1)

                    def loss_fn(weight, probs, reverse_api_end_index):
                        probs = probs[:, -reverse_api_end_index : ]
                        return (weight * -log(probs)).sum(dim = -1)
                    
                    loss = loss_fn(weights_and_zeros_padding, probs, num_to_keep)
                    loss_without_api_response = loss_fn(weights_and_zeros_padding, probs_without_api_response, num_to_keep)
                    loss_with_api_response = loss_fn(weights_and_zeros_padding, probs_with_api_response, num_to_keep)
                    # Comparing each generation to find the best_loss

                    temp = loss_without_api_response - loss_with_api_response
                    if (
                        min(loss_without_api_response, loss)
                        - loss_with_api_response
                        > best_loss
                    ):
                        best_output = generated_texts[j][-2]
                        best_loss = loss_without_api_response - loss_with_api_response

                if len(generated_texts) > 0:
                    outputs[i] = best_output
                    outputs[i]["Score"] = float(best_loss.item())
                    outputs[i]["base_api_loss"] = float(666)
                    del outputs[i]["base_outputs"]
                else:
                    outputs[i] = None
        return outputs

                # generated_texts, test_outputs, base_outputs = self.get_logits(
                #     model,
                #     generated_texts,
                #     max_token_len,
                #     max_token_len_base
                # )

                    # print("hoo ya")
            
            # self.filter_api(
            #     model,
            #     outputs,
            #     max_token_len,
            #     max_token_len_base
            # )





        #         # shape the batches...
        #         for j in range(len(generated_texts)):
        #             # Adding zeros to ensure each example of test_outputs & base_outputs has a length of max_token_len
        #             generated_texts[j].append(
        #                 max_token_len - generated_texts[j][0].shape[1]
        #             )
        #             if generated_texts[j][-1] != 0:
        #                 generated_texts[j][0] = torch.cat(
        #                     (
        #                         generated_texts[j][0],
        #                         torch.zeros(
        #                             (1, generated_texts[j][-1]),
        #                             dtype=generated_texts[j][0].dtype,
        #                             device=generated_texts[j][0].device,
        #                         ),
        #                     ),
        #                     dim=1,
        #                 )
        #             generated_texts[j].append(
        #                 max_token_len_base - generated_texts[j][1].shape[1]
        #             )
        #             if generated_texts[j][-1] != 0:
        #                 generated_texts[j][1] = torch.cat(
        #                     (
        #                         generated_texts[j][1],
        #                         torch.zeros(
        #                             (1, generated_texts[j][-1]),
        #                             dtype=generated_texts[j][1].dtype,
        #                             device=generated_texts[j][1].device,
        #                         ),
        #                     ),
        #                     dim=1,
        #                 )
        #         # Putting the test_outputs into a single tensor
        #         test_outputs = model(
        #             torch.cat(
        #                 list(generated_text[0] for generated_text in generated_texts),
        #                 dim=0,
        #             )
        #         ).logits
        #         # Putting the base_outputs into a single tensor
        #         base_outputs = model(
        #             torch.cat(
        #                 list(generated_text[1] for generated_text in generated_texts),
        #                 dim=0,
        #             )
        #         ).logits
        #         # Varibles to be compared against
        #         best_loss = -99.0
        #         best_output = outputs[i][0]
        #         for j in range(len(generated_texts)):
        #             num_to_keep = generated_texts[j][2]
        #             if generated_texts[j][-2] != 0:
        #                 test = test_outputs[j][: -generated_texts[j][-2]]
        #                 test_loss = criterion(
        #                     # test[-num_to_keep : -(num_to_keep - M)].view(
        #                     test[-num_to_keep : ].view(
        #                         -1, generated_texts[j][-3]["base_outputs"].size(-1)
        #                     ),
        #                     # labels[:, -num_to_keep : -(num_to_keep - M)]
        #                     labels[:, -num_to_keep : ]
        #                     .to(device)
        #                     .view(-1),
        #                 )
        #                 # convert test & label to words
        #                 decoded = []
        #                 for token in labels:
        #                     decoded.append(tokenizer.decode(token))
        #                 print(decoded)

        #                 test = test.softmax(dim = -1)
        #                 predicted_token_ids = torch.argmax(test, dim=-1)
        #                 predicted_tokens = tokenizer.batch_decode(predicted_token_ids)
        #                 # Add all the predicted tokens to a single string
        #                 predicted_string = ""
        #                 for token in predicted_tokens:
        #                     predicted_string += token
        #                 print(predicted_string)

        #             else:
        #                 # decoded = tokenizer.decode(test_outputs[j][-num_to_keep : -(num_to_keep - M)])
        #                 # shape (16, 50257) & (16)
        #                 test_loss = criterion(
        #                     # test_outputs[j][-num_to_keep : -(num_to_keep - M)].view(
        #                     test_outputs[j][-num_to_keep : ].view(
        #                         -1, generated_texts[j][-3]["base_outputs"].size(-1)
        #                     ),
        #                     # labels[:, -num_to_keep : -(num_to_keep - M)]
        #                     labels[:, -num_to_keep : ]
        #                     .to(device)
        #                     .view(-1),
        #                 )
        #             if generated_texts[j][-1] != 0:
        #                 base = base_outputs[j][: -generated_texts[j][-1]]
        #                 base_loss = criterion(
        #                     # base[-num_to_keep : -(num_to_keep - M)].view(
        #                     base[-num_to_keep : ].view(
        #                         -1, generated_texts[j][-3]["base_outputs"].size(-1)
        #                     ),
        #                     # labels[:, -num_to_keep : -(num_to_keep - M)]
        #                     labels[:, -num_to_keep : ]
        #                     .to(device)
        #                     .view(-1),
        #                 )
        #             else:
        #                 base_loss = criterion(
        #                     # base_outputs[j][-num_to_keep : -(num_to_keep - M)].view(
        #                     base_outputs[j][-num_to_keep : ].view(
        #                         -1, generated_texts[j][-3]["base_outputs"].size(-1)
        #                     ),
        #                     # labels[:, -num_to_keep : -(num_to_keep - M)]
        #                     labels[:, -num_to_keep : ]
        #                     .to(device)
        #                     .view(-1),
        #                 )
        #             generated_texts[j][-3]["generated_text"] = generated_texts[j][-3][
        #                 "generated_text"
        #             ].replace(start_str, "")
        #             # Comparing each generation to find the best_loss
        #             if (
        #                 min(base_loss.item(), generated_texts[j][-3]["base_loss"])
        #                 - test_loss
        #                 > best_loss
        #             ):
        #                 best_output = generated_texts[j][-3]
        #                 best_loss = generated_texts[j][-3]["base_loss"] - test_loss
        #         if len(generated_texts) > 0:
        #             outputs[i] = best_output
        #             outputs[i]["Score"] = float(best_loss.item())
        #             outputs[i]["base_api_loss"] = float(base_loss.item())
        #             del outputs[i]["base_outputs"]
        #         else:
        #             outputs[i] = None
        # print(json.dumps(outputs, indent=2))
        # return outputs

    def parse_article(
        self, data: dict, model: PreTrainedModel, tokenizer: PreTrainedTokenizerBase, device
    ):
        """
        Takes in data dict and parses it into API continuations
        :param data: data, assuming it's from load_dataset and has a text field
        :param model:
        :param tokenizer:
        :return: outputs for the input data, should have index of API call insertion, API, and score value at minimum.
        """
        raise NotImplementedError("Fill this in for what you need to do please!")
