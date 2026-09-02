from app.database import Database, get_database


def init_database(
    database: Database | None = None,
) -> None:
    """Create the schema for the given database. Safe to run repeatedly."""
    target = database or get_database()

    target.create_all()


if __name__ == "__main__":
    init_database()
