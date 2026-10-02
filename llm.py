import os

from groq import Groq

from env_config import load_project_env, secret_is_configured

# Make llm.py safe to import/call directly from tests, scripts, or an IDE even
# when app.py was not imported first.
load_project_env()


def _client() -> Groq:
    if not secret_is_configured("GROQ_API_KEY"):
        raise RuntimeError(
            "GROQ_API_KEY is not configured in the project .env (or process environment). "
            "Deterministic SolarGrid tools remain available, but LLM agent chat is disabled."
        )
    return Groq(api_key=os.getenv("GROQ_API_KEY", "").strip())


def call_llm(messages, tools=None, tool_choice=None):
    kwargs = {
        "model": os.getenv("LLM_MODEL_NAME", "openai/gpt-oss-20b").strip(),
        "messages": messages,
    }
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    return _client().chat.completions.create(**kwargs)
