# Lenny Tests

This directory contains all the automated tests for the Lenny application.

## Running Tests

To run the tests, navigate to the root directory of the project and execute the following command:

```bash
pytest
```

This command will discover and run all tests within this directory and its subdirectories.

## Test Structure

Tests are organized based on the modules they are testing. For example, tests for core functionalities are located in `test_core_items.py`.

## Dependencies

Ensure you have all the development dependencies installed, including `pytest` and any plugins specified in `pytest.ini` or `requirements.txt`. You can typically install these using:

```bash
pip install -r requirements.txt 
# or if you have a specific dev requirements file
# pip install -r requirements-dev.txt 
```

## Configuration

Test-specific configurations, such as environment variables for testing, can be managed via a `.env.test` file in the root directory, which is loaded by `pytest-env` (if configured in `pytest.ini`).

The tests utilize an in-memory SQLite database to ensure isolation and speed, avoiding the need for external database services like Docker during testing. Mocking is used for external services like S3 (MinIO) to ensure tests are self-contained and reliable.
