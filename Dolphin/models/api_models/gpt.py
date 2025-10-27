import asyncio
import base64
import os
import pathlib
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError

T = TypeVar("T")

# get openai api key
# API_KEY = os.getenv("OPENAI_API_KEY")


def is_openai_reasoning_model(model_name: str) -> bool:
    """
    例:
        >>> should_skip("o4-mini")          # True
        >>> should_skip("foo-o3-bar")       # True
        >>> should_skip("model_o12large")   # True
        >>> should_skip("gpt-4o-mini")      # False  (o の前に数字がある)
        >>> should_skip("oracle")           # False
    """
    pattern = re.compile(r"\bo\d+\b")
    return bool(pattern.search(model_name))


class GPT4o:
    """
    A GPT-4o model wrapper for handling text and image inputs and generating responses.

    Args:
        pretrained (str): The version of the model to use (default: "gpt-4o-mini").

    Example usage:
    >>> model = GPT4o(pretrained="gpt-4o-mini")
    >>> text = "Describe this image: <image>"
    >>> images = ["image1.jpg"]
    >>> response = model.generate_response(text, images)
    >>> print(response)
    "This is an image of a beautiful sunset."
    """

    def __init__(
        self,
        pretrained: str = "gpt-4.1-mini",
        image_dir: str = None,
        api_key: str = None,
        **kwargs,
    ) -> None:
        super().__init__()
        # Define the image token placeholder for GPT-4 Vision models
        self.pretrained = pretrained
        self.image_token = "<image>"
        self.image_dir = image_dir
        if api_key is None:
            # If no API key is provided, try to get it from environment variables
            self.api_key = os.getenv("OPENAI_API_KEY")
        else:
            self.api_key = api_key

    @staticmethod
    def rename_key_for_openai_api(gen_kwargs: dict) -> dict:
        gen_kwargs["max_completion_tokens"] = gen_kwargs.pop("max_new_tokens")
        gen_kwargs.pop("do_sample", None)
        gen_kwargs.pop("num_beams", None)
        return gen_kwargs

    @staticmethod
    def image_to_base64(input_path: str, image_dir: str) -> str:
        """
        Converts an image from either a local file path or a URL to a Base64 string.

        Args:
            input_path (str): The file path or URL of the image.

        Returns:
            str: Base64 encoded string of the image.

        Raises:
            FileNotFoundError: If the file path does not exist.
            Exception: If the image cannot be fetched from the URL.

        Example:
        >>> base64_img = GPT4o.image_to_base64("/path/to/image.jpg", "data")
        >>> print(base64_img[:30])
        "iVBORw0KGgoAAAANSUhEUgAAABQAAAAUC"
        """
        if os.path.exists(str(pathlib.Path(image_dir).joinpath(input_path))):
            input_path = str(pathlib.Path(image_dir).joinpath(input_path))
            # Read image from local file path
            with open(input_path, "rb") as image_file:
                image_data = image_file.read()
        else:
            print(str(pathlib.Path(image_dir).joinpath(input_path)))
            raise FileNotFoundError(
                f"The file path does not exist: {input_path}"
            ) from None

        # Encode image data to Base64
        base64_str = base64.b64encode(image_data).decode("utf-8")
        return base64_str

    @staticmethod
    def create_system_prompt(system_prompt: str) -> list[dict[str, Any]]:
        """
        Creates a system prompt for the model.

        Args:
            system_prompt (str): The system-level instruction or information.

        Returns:
            List[Dict[str, Any]]: A list containing the system prompt message.

        Example:
        >>> system_prompt = "You are a helpful assistant."
        >>> messages = GPT4o.create_system_prompt(system_prompt)
        >>> print(messages)
        [{'role': 'system', 'content': 'You are a helpful assistant.'}]
        """
        return [{"role": "system", "content": system_prompt}]

    @staticmethod
    def create_zero_shot_prompt(
        zero_shot_data: dict[str, Any], img_dir: str = None
    ) -> list[dict[str, Any]]:
        """
        Creates a zero-shot prompt for the model with optional images.

        Args:
            zero_shot_data (Dict[str, Any]): Dictionary containing text input and optional images.

        Returns:
            List[Dict[str, Any]]: A list of message contents including both text and images.

        Example:
        >>> zero_shot_data = {"user": "What is in this image?", "images": ["/path/to/image.jpg"]}
        >>> messages = GPT4o.create_zero_shot_prompt(zero_shot_data)
        >>> print(messages)
        [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,...'}}, {'type': 'text', 'text': 'What is in this image?'}]}]
        """
        messages = []
        role = "user"
        content = []

        if zero_shot_data.get("images"):
            # Convert images to Base64 and add to content
            for img in zero_shot_data["images"]:
                base64_img = GPT4o.image_to_base64(img, img_dir)
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"},
                    }
                )

        # Add user text
        content.append({"type": "text", "text": zero_shot_data[role]})
        messages.append({"role": role, "content": content})

        return messages

    @staticmethod
    def create_few_shot_prompt(
        few_shot_data: list[dict[str, Any]], img_dir: str = None
    ) -> list[dict[str, Any]]:
        """
        Creates a few-shot prompt for the model with both user and assistant examples.

        Args:
            few_shot_data (List[Dict[str, Any]]): List of few-shot examples, including user inputs and assistant responses.

        Returns:
            List[Dict[str, Any]]: A list of message contents with examples for few-shot learning.

        Example:
        >>> few_shot_data = [
                {"user": "What is this?", "assistant": "This is a cat.", "images": ["/path/to/image.jpg"]}
            ]
        >>> messages = GPT4o.create_few_shot_prompt(few_shot_data)
        >>> print(messages)
        [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,...'}}, {'type': 'text', 'text': 'What is this?'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'This is a cat.'}]}]
        """
        messages = []
        for shot_data in few_shot_data:
            # User input with optional images
            role = "user"
            content = []
            if shot_data.get("images"):
                for img in shot_data["images"]:
                    base64_img = GPT4o.image_to_base64(img, img_dir)
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_img}"
                            },
                        }
                    )
            content.append({"type": "text", "text": shot_data[role]})
            messages.append({"role": role, "content": content})

            # Assistant response
            role = "assistant"
            content = [{"type": "text", "text": shot_data[role]}]
            messages.append({"role": role, "content": content})

        return messages

    @staticmethod
    async def retry_on_error(
        openai_call: Callable[[], Awaitable[T]],
        max_num_trials: int = 5,
        first_wait_time: int = 10,
    ) -> Awaitable[T]:
        """
        Retries OpenAI API calls on errors such as connection issues, rate limits, and API status errors.

        Args:
            openai_call (Callable[[], Awaitable[T]]): The OpenAI API call to retry.
            max_num_trials (int): Maximum number of retry attempts (default: 5).
            first_wait_time (int): Initial wait time in seconds before retrying (default: 10).

        Returns:
            Awaitable[T]: The result of the successful API call.

        Raises:
            APIConnectionError, RateLimitError, APIStatusError: If all retries fail.

        Example:
        >>> result = await GPT4o.retry_on_error(my_openai_call, max_num_trials=3)
        """
        for i in range(max_num_trials):
            try:
                return await openai_call()  # Execute OpenAI API call
            except (APIConnectionError, RateLimitError, APIStatusError) as e:
                if i == max_num_trials - 1:
                    raise  # Raise error after final attempt
                print(f"Error received: {e}")
                if e.__class__.__name__ == "NotFoundError":
                    raise ValueError(
                        "The specified model may not be supported."
                    ) from None
                wait_time_seconds = first_wait_time * (2**i)
                print(f"Waiting for {wait_time_seconds} seconds before retrying...")
                await asyncio.sleep(wait_time_seconds)

    async def _async_batch_run_chatgpt(
        self, messages: list[dict[str, Any]], **gen_kwargs
    ) -> list[str]:
        """
        Asynchronously sends multiple requests to the OpenAI API with error handling.

        Args:
            messages (List[Dict[str, Any]]): List of message prompts to send to the API.
            gen_kwargs (Dict[str, Any]): Generation parameters for the API.

        Returns:
            List[str]: List of generated responses from the API.

        Example:
        >>> responses = await GPT4o._async_batch_run_chatgpt(messages=[{"role": "user", "content": "Tell me a joke."}])
        >>> print(responses)
        ["Why did the chicken cross the road? To get to the other side!"]
        """
        client = AsyncOpenAI(api_key=self.api_key)
        tasks = [
            GPT4o.retry_on_error(
                openai_call=lambda x=ms: client.chat.completions.create(
                    messages=x, **gen_kwargs
                )
            )
            for ms in messages
        ]
        completions = await asyncio.gather(*tasks)  # Execute all tasks asynchronously
        return [c.choices[0].message.content for c in completions]

    def batch_run_chatgpt(
        self, messages: list[dict[str, Any]], **gen_kwargs
    ) -> list[str]:
        """
        Runs asynchronous OpenAI API requests in a synchronous wrapper using asyncio.

        Args:
            messages (List[Dict[str, Any]]): List of message prompts.
            gen_kwargs (Dict[str, Any]): Generation parameters for the API.

        Returns:
            List[str]: List of generated responses.

        Example:
        >>> responses = GPT4o.batch_run_chatgpt(messages=[{"role": "user", "content": "Tell me a joke."}])
        >>> print(responses)
        ["Why did the chicken cross the road? To get to the other side!"]
        """
        return asyncio.run(self._async_batch_run_chatgpt(messages, **gen_kwargs))

    def generate_response(
        self,
        text: str,
        images: list[str] | None = None,
        system_prompt: str | None = None,
        few_shot_data: list[dict[str, Any]] | None = None,
        gen_kwargs: dict | None = None,
    ) -> str:
        """
        Generates a response based on the provided text and optional images.

        Args:
            text (str): The input text prompt.
            images (Optional[List[str]]): List of image file paths or URLs (optional).
            system_prompt (Optional[str]): System-level instruction or context (optional).
            few_shot_data (Optional[List[Dict[str, Any]]]): Few-shot examples to include in the conversation (optional).
            gen_kwargs (Optional[dict]): Additional generation parameters (optional).

        Returns:
            str: The generated response text.

        Example usage:
        >>> model = GPT4o()
        >>> response = model.generate_response("Describe this image: <image>", ["image1.jpg"])
        >>> print(response)
        "This is an image of a beautiful sunset."
        """
        # breakpoint()
        gen_kwargs = gen_kwargs if gen_kwargs is not None else {}
        # rename key for openai api
        if gen_kwargs.get("max_new_tokens", None):
            gen_kwargs = GPT4o.rename_key_for_openai_api(gen_kwargs)

        if images is not None:
            # Validate number of image tokens in text
            img_token_num = text.count(self.image_token)
            if img_token_num > len(images):
                raise ValueError(
                    f"Error! The number of image tokens (img_token_num={img_token_num}) is greater than the number of input images (len(images)={len(images)})."
                ) from None

        message = []

        if system_prompt:
            message += GPT4o.create_system_prompt(system_prompt)

        if few_shot_data:
            message += GPT4o.create_few_shot_prompt(few_shot_data, self.image_dir)

        message += GPT4o.create_zero_shot_prompt(
            {"user": text, "images": images}, self.image_dir
        )

        # Set default generation parameters if not provided
        gen_kwargs.setdefault("model", self.pretrained)
        ## An upper bound for the number of tokens that can be generated for a completion, including visible output tokens and reasoning tokens.
        ## reasoning tokens: https://platform.openai.com/docs/guides/reasoning
        gen_kwargs.setdefault("max_completion_tokens", 16384)
        ## How many chat completion choices to generate for each input message.
        ## Note that you will be charged based on the number of generated tokens across all of the choices. Keep `n` as `1` to minimize costs.
        gen_kwargs.setdefault("n", 1)
        ## Number between -2.0 and 2.0. Positive values penalize new tokens based on whether they appear in the text so far, increasing the model's likelihood to talk about new topics.
        gen_kwargs.setdefault("presence_penalty", 0)
        ## Number between -2.0 and 2.0. Positive values penalize new tokens based on their existing frequency in the text so far, decreasing the model's likelihood to repeat the same line verbatim.
        ## See more information about frequency and presence penalties (https://platform.openai.com/docs/guides/text-generation/parameter-details).
        gen_kwargs.setdefault("frequency_penalty", 0)
        ## Modify the likelihood of specified tokens appearing in the completion.
        ## Accepts a JSON object that maps tokens (specified by their token ID in the tokenizer) to an associated bias value from -100 to 100. Mathematically, the bias is added to the logits generated by the model prior to sampling.
        ## The exact effect will vary per model, but values between -1 and 1 should decrease or increase likelihood of selection; values like -100 or 100 should result in a ban or exclusive selection of the relevant token.
        gen_kwargs.setdefault("logit_bias", None)
        ## Whether to return log probabilities of the output tokens or not. If true, returns the log probabilities of each output token returned in the `content` of `message`.
        gen_kwargs.setdefault("logprobs", False)
        ## An integer between 0 and 20 specifying the number of most likely tokens to return at each token position, each with an associated log probability. `logprobs` must be set to `true` if this parameter is used.
        gen_kwargs.setdefault("top_logprobs", None)
        ## This feature is in Beta. If specified, our system will make a best effort to sample deterministically, such that repeated requests with the same `seed` and parameters should return the same result.
        ## Determinism is not guaranteed, and you should refer to the `system_fingerprint` response parameter to monitor changes in the backend.
        gen_kwargs.setdefault("seed", 42)
        ## Up to 4 sequences where the API will stop generating further tokens. (string / array / null)
        # gen_kwargs.setdefault("stop", null)
        ## What sampling temperature to use, between 0 and 2. Higher values like 0.8 will make the output more random, while lower values like 0.2 will make it more focused and deterministic.
        ## We generally recommend altering this or `top_p` but not both. (number or null, Defaults to 1)
        if is_openai_reasoning_model(self.pretrained):
            if "temperature" in gen_kwargs:
                del gen_kwargs["temperature"]
                print('Warning: Delete "temperature" key in gen_kwargs')
        else:
            gen_kwargs.setdefault("temperature", 0.0)
        ## An alternative to sampling with temperature, called nucleus sampling, where the model considers the results of the tokens with top_p probability mass. So 0.1 means only the tokens comprising the top 10% probability mass are considered.
        ## We generally recommend altering this or `temperature`` but not both. (number or null, Defaults to 1)
        gen_kwargs.setdefault("top_p", 1.0)

        # Perform inference
        generated_texts = self.batch_run_chatgpt(
            messages=[message],
            **gen_kwargs,
        )

        return generated_texts[0]  # Return the first response

    # 非同期バッチ処理のタスクコントローラー
    async def _async_generate_batch_response(
        self, batch_inputs: list[dict[str, Any]]
    ) -> list[str]:
        """
        Args:
            batch_inputs: [
                {
                    "text": str,
                    "images": list[str] | None,
                    "system_prompt": str | None,
                    "few_shot_data": list[dict[str, Any]] | None,
                    "gen_kwargs": dict | None
                },
                ...
            ]

        Returns:
            List[str]: 各入力に対応する生成結果（入力順）
        """
        client = AsyncOpenAI(api_key=self.api_key)
        tasks = []

        for item in batch_inputs:
            # ---------------- メッセージ構築 ----------------
            text = item["text"]
            images = item.get("images")
            system_prompt = item.get("system_prompt")
            few_shot_data = item.get("few_shot_data")
            gen_kwargs = (item.get("gen_kwargs") or {}).copy()

            # openai 用キー変換
            if "max_new_tokens" in gen_kwargs:
                gen_kwargs = self.rename_key_for_openai_api(gen_kwargs)

            # メッセージリスト
            message = []
            if system_prompt:
                message += self.create_system_prompt(system_prompt)
            if few_shot_data:
                message += self.create_few_shot_prompt(few_shot_data, self.image_dir)
            message += self.create_zero_shot_prompt(
                {"user": text, "images": images}, self.image_dir
            )

            # ---------------- 生成パラメータ ----------------
            gen_kwargs.setdefault("model", self.pretrained)
            gen_kwargs.setdefault("max_completion_tokens", 16384)
            gen_kwargs.setdefault("n", 1)
            gen_kwargs.setdefault("presence_penalty", 0)
            gen_kwargs.setdefault("frequency_penalty", 0)
            gen_kwargs.setdefault("logit_bias", None)
            gen_kwargs.setdefault("logprobs", False)
            gen_kwargs.setdefault("top_logprobs", None)
            gen_kwargs.setdefault("seed", 42)

            if is_openai_reasoning_model(self.pretrained):
                gen_kwargs.pop("temperature", None)  # reasoning モデルでは温度指定不可
            else:
                gen_kwargs.setdefault("temperature", 0.0)

            gen_kwargs.setdefault("top_p", 1.0)

            # ----------------  API 呼び出しタスク登録 ----------------
            tasks.append(
                GPT4o.retry_on_error(
                    openai_call=lambda m=message,
                    kw=gen_kwargs: client.chat.completions.create(messages=m, **kw)
                )
            )

        # 並列実行 & 結果取得
        completions = await asyncio.gather(*tasks)
        return [c.choices[0].message.content for c in completions]

    def generate_batch_response(self, batch_inputs: list[dict[str, Any]]) -> list[str]:
        """
        同期 API。内部で asyncio を回して非同期バッチ実行する。
        """
        return asyncio.run(self._async_generate_batch_response(batch_inputs))
