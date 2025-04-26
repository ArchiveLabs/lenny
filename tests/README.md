# Lenny Tests

This directory contains tests for the Lenny application. Follow the instructions below to run the tests on your machine.

## Prerequisites

Before running tests, make sure you have:

- Python 3.x installed
- Docker and Docker Compose installed (for integration tests)
- Git (to clone the repository if you haven't already)

## Setup for Testing

1. **Clone the repository** (if you haven't already):
   ```bash
   git clone https://github.com/ArchiveLabs/lenny.git
   cd lenny
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   pip install -e .  # Install the project in development mode
   pip install pytest pytest-cov  # Make sure test dependencies are installed
   ```

4. **Set up test environment variables**:
```
   /lenny/.env.test
```
```
   create `.env.test` to have appropriate test settings:
   ```
   ```
  TESTING=true
  DB_URI=sqlite:///:memory:
  S3_ACCESS_KEY=lenny
  S3_SECRET_KEY=lennytesting
  MINIO_BUCKET=lenny
  MINIO_HOST=localhost
  MINIO_PORT=9000
  POSTGRES_HOST=localhost
  POSTGRES_USER=lenny
  POSTGRES_PASSWORD=lennytest
  POSTGRES_PORT=5432
  POSTGRES_DB=lending_system 
   ```

## Running the Tests

### Unit Tests

Unit tests can be run without a running server:

```bash
python -m pytest tests/test_core_items.py -v
```

### Run Specific Tests

To run a specific test file:
```bash
python -m pytest tests/test_core_items.py
```

To run a specific test function:
```bash
python -m pytest tests/test_core_items.py::test_upload_item_success
```

### Run with Coverage

To run tests with coverage reporting:
```bash
python -m pytest --cov=lenny
```

Generate an HTML coverage report:
```bash
python -m pytest --cov=lenny --cov-report=html
```

## Test Structure

- **Unit Tests**: Test individual components in isolation (using mocks where appropriate)
- **Integration Tests**: Test how components work together

## Notes for Specific Tests

### `test_core_items.py`

This test module verifies the file upload, deletion, and access management functionality. The tests:

- Mock MinIO operations so no actual S3 storage is needed
- Create temporary files for testing uploads
- Verify correct file extensions (.pdf, .epub) are preserved when storing files
- Check that database records are properly updated

When running these tests, make sure:
- You have an active Python environment with all dependencies installed
- The `.env.test` file is properly configured
- You're in the project root directory when running pytest commands

## Troubleshooting

If you encounter issues running tests:

1. **Database Connection Errors**: Make sure the test database is properly configured in `.env.test` 

2. **Import Errors**: Make sure you've installed the project in development mode with `pip install -e .`

3. **MinIO Connection Errors**: These shouldn't occur as the tests mock MinIO operations, but check your environment variables if you see them

4. **File Not Found Errors**: Make sure you're running the tests from the project root directory