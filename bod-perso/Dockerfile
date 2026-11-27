# BOD Perso - image du Cloud Run Job "bod-collect"
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# libgomp : requis par scikit-learn (HDBSCAN) sur l'image slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY sql/ ./sql/

RUN useradd --create-home --uid 10001 bod
USER bod

# L'étape est choisie par la variable STAGE (collect | cluster | opportunities | simulate | serp | all | init)
CMD ["python", "-m", "app.main"]
