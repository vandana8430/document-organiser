"""Exercise SmartFiles' configured chat provider without printing credentials."""
from services.llm_service import ModelUnavailable, generate_answer


if __name__ == "__main__":
    try:
        print(generate_answer("What can SmartFiles do?", project_context=True))
    except ModelUnavailable as error:
        print(f"SmartFiles AI is unavailable: {error}")
