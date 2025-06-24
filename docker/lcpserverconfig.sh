#!/bin/bash

set -o allexport
source .env
set +o allexport

mkdir -p ./configs/lcp/processed

envsubst < lcpserver.yaml.template > ./configs/lcp/processed/lcpserver.yaml
