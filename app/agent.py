from app.model_provider import ModelProvider


class AgentService:

    def __init__(self, model: ModelProvider) -> None:
        self.model = model

    def run(self, message: str) -> str:
        return self.model.generate(message)