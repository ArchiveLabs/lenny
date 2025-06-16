#!/usr/bin/env bash

./docker/configure.sh

MODE=
LOG=
REBUILD=false
PRELOAD=""

# Parse cli args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild)
      REBUILD=true
      shift
      ;;
    --preload)
      # If next arg exists and is a value (not another flag)
      if [[ -n "$2" && "$2" != --* ]]; then
        PRELOAD="$2"
        shift 2
      else
        PRELOAD=true
        shift
      fi
      ;;
    --dev)
      MODE="dev"
      shift
      ;;
    --log)
      LOG=true
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      shift
      ;;
  esac
done


if [[ "$MODE" == "dev" ]]; then
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

    if [[ "$REBUILD" == "true" ]]; then
	echo "Performing full rebuild..."
	docker compose down --volumes --remove-orphans
        docker compose build --no-cache
        docker compose up -d
    else
	docker-compose -p lenny up -d
    fi


    if [[ -n "$PRELOAD" ]]; then

	echo "[+] Waiting up to 30 seconds for services to start and pass health checks..."
	for i in {1..15}; do
	    docker ps -f "name=lenny_api" -f status=running -q | grep -q .
	    sleep 2
	    [[ $i -eq 15 ]] && { echo "[!] Error: Lenny did not launch after 30 seconds."; exit 1; }
	done

	if [[ "$PRELOAD" =~ ^[0-9]+$ ]]; then
	    EST_MIN=$(echo "scale=2; (800 / $PRELOAD) / 60" | bc)
	    LIMIT="-n $PRELOAD"
	else
	    EST_MIN=$(echo "scale=2; 800 / 60" | bc)
	    LIMIT=""
	fi

	echo "[+] Preloading ${PRELOAD:-ALL}/~800 book(s) from StandardEbooks (~$EST_MIN minutes)..."
	docker exec -it lenny_api python scripts/preload.py $LIMIT
    fi

    if [[ "$LOG" == "true" ]]; then
	docker compose logs -f
    fi
fi
