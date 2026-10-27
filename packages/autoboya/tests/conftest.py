from datetime import datetime

import pytest


@pytest.fixture
def semester_config():
    year = datetime.now().year
    return {"data": {"semester": [{
        "semesterStartDate": f"{year}-01-01 00:00:00",
        "semesterEndDate": f"{year}-12-31 23:59:59",
    }]}}
