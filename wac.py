from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from jsonget import json_get, json_get_default
from typing import Optional
import json
import logging
import requests

from datetime import datetime
from decouple import config
import typesense

HA_URL = config('HA_URL', default="http://homeassistant.local:8123", cast=str)
HA_TOKEN = config('HA_TOKEN', default=None, cast=str)
LOG_LEVEL = config('LOG_LEVEL', default="debug", cast=str)
TGI_URL = config(f'TGI_URL', default=None, cast=str)

# This doesn't seem to be getting from docker to here - FIX
TYPESENSE_API_KEY = config('TYPESENSE_API_KEY', default='testing', cast=str)
TYPESENSE_HOST = config('TYPESENSE_HOST', default='127.0.0.1', cast=str)
TYPESENSE_PORT = config('TYPESENSE_PORT', default=8108, cast=int)

HA_URL = f'{HA_URL}/api/conversation/process'
HA_TOKEN = f'Bearer {HA_TOKEN}'

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')

app = FastAPI(title="WAC Proxy",
              description="Make voice better",
              version="0.1",
              openapi_url="/openapi.json",
              docs_url="/docs",
              redoc_url="/redoc")

log = logging.getLogger("WAC")
try:
    log.setLevel(LOG_LEVEL).upper()
except:
    pass

# Basic stuff we need
ha_headers = {
    "Authorization": HA_TOKEN,
}

typesense_client = typesense.Client({
    'nodes': [{
        'host': TYPESENSE_HOST,
        'port': TYPESENSE_PORT,
        'protocol': 'http'
    }],
    'api_key': TYPESENSE_API_KEY,
    'connection_timeout_seconds': 1
})

# WAC Search


def wac_search(command, exact_match=False, distance=2, num_results=5):
    log.info(f"WAC Search distance is {distance}")
    # Set fail by default
    success = False
    wac_command = command
    search_parameters = {
        'q': command,
        'query_by': 'command',
        'sort_by': '_text_match:desc,rank:desc',
        'text_match_type': 'max_score',
        'prioritize_token_position': False,
        'drop_tokens_threshold': 1,
        'typo_tokens_threshold': 2,
        'split_join_tokens': 'fallback',
        'num_typos': distance,
        'min_len_1typo': 1,
        'min_len_2typo': 1,
        'per_page': num_results
    }

    if exact_match:
        log.info(f"Doing exact match WAC Search")
        search_parameters.update({'filter_by': f'command:={command}'})

    # Try WAC search
    try:
        log.info(f"Doing WAC Search for command: {command}")
        wac_search_result = typesense_client.collections['commands'].documents.search(
            search_parameters)
        text_score = json_get(wac_search_result, "/hits[0]/text_match")
        tokens_matched = json_get(
            wac_search_result, "/hits[0]/text_match_info/tokens_matched")
        wac_command = json_get(wac_search_result, "/hits[0]/document/command")
        source = json_get(wac_search_result, "/hits[0]/document/source")
        success = True
    except:
        pass

    return success, wac_command

# WAC Add


def wac_add(command):
    log.info(f"Doing WAC Add for command: {command}")
    try:
        log.info(f"Search WAC before adding command: {command}")
        wac_exact_search_status, wac_command = wac_search(
            command, exact_match=True)
        if wac_exact_search_status is True:
            log.info('Not adding duplicate command')
            return

        command_json = {
            'command': command,
            'rank': 1.0,
            'source': 'autolearn',
        }
        # Use create to update in real time
        typesense_client.collections['commands'].documents.create(command_json)
        log.info(f'Added WAC command: {command}')
    except:
        log.error(f"WAC Add for command: {command} failed!")

    return

# Request coming from proxy


def api_post_proxy_handler(command, language):

    # Init speech for when all else goes wrong
    speech = "Sorry, I don't know that command."

    try:
        ha_data = {"text": command, "language": language}
        ha_response = requests.post(HA_URL, headers=ha_headers, json=ha_data)
        ha_response = ha_response.json()
        code = json_get_default(
            ha_response, "/response/data/code", "intent_match")

        if code == "no_intent_match":
            log.info('No HA Intent Match')
        else:
            log.info('HA Intent Match')
            wac_add(command)
            # Set speech to HA response and return
            log.info('Setting speech to HA response')
            speech = json_get(
                ha_response, "/response/speech/plain/speech", str)
            return speech
    except:
        pass

    # Do WAC Search
    wac_success, wac_command = wac_search(
        command, exact_match=False, distance=2, num_results=5)

    if wac_success:

        # Re-run HA with WAC Command
        try:
            ha_data = {"text": wac_command, "language": language}
            ha_response = requests.post(
                HA_URL, headers=ha_headers, json=ha_data)
            ha_response = ha_response.json()
            code = json_get_default(
                ha_response, "/response/data/code", "intent_match")

            if code == "no_intent_match":
                log.info(f'No WAC Command HA Intent Match: {wac_command}')
            else:
                log.info(f'WAC Command HA Intent Match: {wac_command}')

            # Set speech to HA response - whatever it is at this point
            log.info('Setting speech to HA response')
            speech = json_get(
                ha_response, "/response/speech/plain/speech", str)

        except:
            pass

    return speech


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/proxy")
async def api_post_proxy(request: Request):
    request_json = await request.json()
    language = request_json['language']
    text = request_json['text']
    response = api_post_proxy_handler(text, language)

    return PlainTextResponse(content=response)
