import os

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

def get_llm() -> BaseChatModel:
	"""Create the Gemini chat model used by the LangGraph agent."""

	api_key = os.getenv("GEMINI_API_KEY")
	if not api_key:
		raise RuntimeError("GEMINI_API_KEY must be set")

	return ChatGoogleGenerativeAI(
		model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
		google_api_key=api_key,
		temperature=0.2,
	)