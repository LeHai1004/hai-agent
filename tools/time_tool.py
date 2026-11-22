from datetime import datetime


def get_current_time() -> str:
    """Get the current date and time."""
    print("  [tool] get_current_time")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
