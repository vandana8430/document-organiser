"""Exercise SmartFiles' configured Gemini client without printing credentials."""
from services.llm_service import ModelUnavailable, generate_answer


if __name__ == "__main__":
    try:
        print(generate_answer("Hello! Explain SmartFiles in two sentences.", project_context=True))
    except ModelUnavailable as error:
        print(f"Gemini request failed: {error}")
