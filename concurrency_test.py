
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone
import threading

BASE_URL = "http://127.0.0.1:8000"
PUNCH_IN_URL = f"{BASE_URL}/attendance/punch-in"
PUNCH_OUT_URL = f"{BASE_URL}/attendance/punch-out"

TOTAL_REQUESTS = 102

barrier = threading.Barrier(TOTAL_REQUESTS)


EMP_CODES = [
    "EMP0001",
    "EMP0002",
    "EMP0003",
    "EMP0004",
    "EMP0005",
    "EMP0006",
]

def punch_in_employee(emp_code):
    payload = {
        "emp_code": emp_code,
        "punched_at": datetime.now(timezone.utc).isoformat(),
        "status": "PRESENT",
    }

    response = requests.post(
        PUNCH_IN_URL,
        json=payload,
        timeout=15,
    )

    return emp_code, response.status_code


with ThreadPoolExecutor(max_workers=TOTAL_REQUESTS) as pool:
    futures = [pool.submit(punch_in_employee, emp_code) for emp_code in EMP_CODES * (TOTAL_REQUESTS // len(EMP_CODES))]
    results = [future.result() for future in as_completed(futures)]

counts = Counter(status for status, _ in results)

print(f"Total requests: {TOTAL_REQUESTS}")
print("Response counts:", counts)

for status, body in results:
    print(f"{status}: {body}")
