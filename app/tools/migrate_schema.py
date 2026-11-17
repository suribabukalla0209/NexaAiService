from app.core.config import Settings
from app.repositories.message_repository import MessageRepository


def main() -> None:
    settings = Settings()
    repository = MessageRepository(settings.database_connection_url)
    try:
        repository.initialize()
        print("Database schema is at the latest Alembic revision.")
    finally:
        repository.database.dispose()


if __name__ == "__main__":
    main()
