#!/bin/bash

./docker/configure.sh

if [[ "$1" == "--dev" ]]; then
    echo "Running in development mode..."
    if [ ! -f ./env/bin/activate ]; then
        virtualenv env
    fi
    source ./env/bin/activate
    pip install --index-url --index-url "${PIP_INDEX_URL:-https://pypi.org/simple}" --no-cache-dir -r requirements.txt
    source ./env/bin/activate
    uvicorn lenny.app:app --reload
else
    echo "Running in production mode..."
    export $(grep -v '^#' .env | xargs)

    mkdir -p ./epubs
    wget -O ./epubs/test_book.epub "https://standardebooks.org/ebooks/l-frank-baum/dorothy-and-the-wizard-in-oz/downloads/l-frank-baum_dorothy-and-the-wizard-in-oz.epub?source=download"
    docker-compose -p lenny up -d
fi
