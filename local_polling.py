from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

from bot import create_application


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    application = create_application()
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
