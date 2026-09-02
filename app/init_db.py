from app.database import Database, get_database
from app.seed_data import seed_migration_batches


def init_database(
    database: Database | None = None,
    seed: bool = False,
) -> int:
    """Create the schema for the given database. Safe to run repeatedly.

    Seeding is opt-in so that programmatic callers (including tests) decide
    explicitly whether development data belongs in their database.

    Returns the number of seed rows inserted.
    """
    target = database or get_database()

    target.create_all()

    if not seed:
        return 0

    return seed_migration_batches(target)


if __name__ == "__main__":
    inserted = init_database(seed=True)

    print(f"Schema ready. Seeded {inserted} migration batch record(s).")
