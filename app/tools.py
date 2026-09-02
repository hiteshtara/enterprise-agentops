def calculator(a: float, b: float, operation: str) -> float:
    if operation == "add":
        return a + b

    if operation == "subtract":
        return a - b

    if operation == "multiply":
        return a * b

    if operation == "divide":
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b

    raise ValueError(f"Unsupported operation: {operation}")


MIGRATION_BATCHES = {
    41: {
        "status": "SUCCESS",
        "records": 499,
        "duration_seconds": 38,
        "error": None,
    },
    42: {
        "status": "SUCCESS",
        "records": 498,
        "duration_seconds": 41,
        "error": None,
    },
    43: {
        "status": "FAILED",
        "records": 495,
        "duration_seconds": 12,
        "error": "Oracle connection timeout",
    },
    44: {
        "status": "SUCCESS",
        "records": 497,
        "duration_seconds": 39,
        "error": None,
    },
}


def get_migration_status(batch_id: int) -> dict:
    if batch_id not in MIGRATION_BATCHES:
        return {
            "batch_id": batch_id,
            "status": "NOT_FOUND",
        }

    return {
        "batch_id": batch_id,
        **MIGRATION_BATCHES[batch_id],
    }
