from abc import ABC, abstractmethod

from openai import OpenAI

DEFAULT_MODEL = "gpt-5.4-mini"


class ModelProvider(ABC):
    @abstractmethod
    def generate(
        self,
        message: str,
    ) -> str:
        pass

    @abstractmethod
    def generate_with_tools(
        self,
        input_items,
        tools,
    ):
        pass


class OpenAIModelProvider(ModelProvider):
    """Calls the OpenAI Responses API.

    The underlying client is built lazily on first use, so importing and wiring
    the application does not require OPENAI_API_KEY. Credential validation still
    happens in full -- it is simply deferred to the first real API call.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client: OpenAI | None = None,
    ) -> None:
        self.model = model
        self._client = client

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI()

        return self._client

    def generate(
        self,
        message: str,
    ) -> str:
        response = self.client.responses.create(
            model=self.model,
            input=message,
        )

        return response.output_text

    def generate_with_tools(
        self,
        input_items,
        tools,
    ):
        return self.client.responses.create(
            model=self.model,
            input=input_items,
            tools=tools,
            parallel_tool_calls=False,
        )
