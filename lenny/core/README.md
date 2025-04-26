# Lenny Core

The Core directory contains the business logic and core functionality of the Lenny Lending System.

## Overview

This directory houses modules that provide the foundational business logic for Lenny's operations. These modules handle the internal processes that power the system's features, separate from the API routes and database models.

## Modules

### Admin (`admin.py`)

This module provides administration functionality:

- `verify_librarian()`: Authenticates librarian credentials against the system's S3 configuration

### Items (`items.py`)

This module handles all item management operations with MinIO S3 storage:

- `initialize_minio_buckets()`: Creates required S3 buckets if they don't exist
- `upload_item()`: Uploads new items to both public and protected buckets based on settings
- `delete_item()`: Removes items from the system and associated S3 storage
- `update_item_access()`: Changes access settings by managing protected/public bucket storage

## Architecture

The core modules follow these design principles:

1. **Separation of Concerns**: Business logic is separated from API routes
2. **Service Layer Pattern**: Core functions act as a service layer between API routes and data models
3. **Infrastructure Abstraction**: Storage operations are abstracted for potential future backends

## Integration

Core functions are used by the API routes to process requests. They interact with:

- Database models via SQLAlchemy sessions
- S3 storage via MinIO client
- Configuration settings from the app's config module