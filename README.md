## Application Overview

This project implements an Employee Attendance & Analytics API using FastAPI and MongoDB. It supports employee management, attendance tracking, punch-in/punch-out, attendance regularization, and analytics.

## Local Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a local `.env` file and configure `MONGO_URI` and `MONGO_DB`.

Do not commit `.env` files, database credentials, or other secrets to the repository.

### 3. Start the application

Run the following command from the project root:

```bash
uvicorn app.main:app --port 8000
```

### 4. API documentation

Once the application is running, open:

`http://127.0.0.1:8000/docs`

## Implementation Notes

- All application code is maintained in `app/main.py`.
- MongoDB is used for employee records, attendance data, and analytics.
- Database indexes support common queries and help prevent duplicate employee and daily attendance records.

## Submission Files

The public repository includes the following required files:

- `app/main.py`
- `requirements.txt`
- `REVIEW.md`
- `DECISIONS.md`
- `README.md`

No `.env` files, database credentials, or other secrets should be committed.
