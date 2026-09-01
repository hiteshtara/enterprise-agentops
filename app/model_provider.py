from abc import ABC, abstractmethod

from openai import OpenAI


class ModelProvider(ABC):

    @abstractmethod
    def generate(self, message: str) -> str:
        pass


class OpenAIModelProvider(ModelProvider):

    def __init__(self) -> None:
        self.client = OpenAI()

    def generate(self, message: str) -> str:
        response = self.client.responses.create(
            model="gpt-5.4-mini",
            input=message,
        )

        return response.output_text