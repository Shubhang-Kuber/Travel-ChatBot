import os

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq

load_dotenv()

def get_llm() -> BaseChatModel:
	"""Create the Groq chat model used by the LangGraph agent."""

	api_key = os.getenv("GROQ_API_KEY")
	if not api_key:
		raise RuntimeError("GROQ_API_KEY must be set")

	return ChatGroq(
		model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
		# gpt-oss thinks before answering; "low" keeps each call fast. Reasoning
		# comes back outside .content, so it never reaches the user.
		reasoning_effort=os.getenv("GROQ_REASONING_EFFORT", "low") or None,
		api_key=api_key,
		temperature=0.2,
		timeout=30,
		max_retries=2,
	)
