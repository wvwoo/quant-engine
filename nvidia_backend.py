
import os

import time

import random

import tiktoken

from openai import OpenAI, RateLimitError, APIStatusError



# SECURITY: a live NVIDIA credential was hardcoded here as the os.environ.get default.
# It was redacted before this file's first commit so the secret never enters git history.
# The key MUST still be treated as leaked and rotated by the owner — it existed in
# plaintext on disk. Fail closed: a missing key is now a crash, not a silent fallback.
NVIDIA_API_KEY = os.environ["NVIDIA_API_KEY"]



client = OpenAI(

    base_url="https://integrate.api.nvidia.com/v1",

    api_key=NVIDIA_API_KEY

)



def count_tokens(messages, model="gpt-4"):

    try:

        encoding = tiktoken.encoding_for_model(model)

    except KeyError:

        encoding = tiktoken.get_encoding("cl100k_base")

    

    num_tokens = 0

    for message in messages:

        num_tokens += 4

        for key, value in message.items():

            safe_value = value.encode('utf-8', errors='ignore').decode('utf-8')

            num_tokens += len(encoding.encode(safe_value))

    num_tokens += 2

    return num_tokens



def safe_predict_nvidia(messages, max_retries=4, max_allowed_tokens=15000):

    current_tokens = count_tokens(messages)

    print("Current Context Volume: " + str(current_tokens) + " Tokens")

    

    while current_tokens > max_allowed_tokens and len(messages) > 1:

        print("Safety Warning: Context too high. Removing oldest message...")

        if messages[0].get("role") == "system":

            messages.pop(1)

        else:

            messages.pop(0)

        current_tokens = count_tokens(messages)



    delay = 4.0

    for attempt in range(max_retries):

        try:

            print("Sending request to NVIDIA (Attempt " + str(attempt + 1) + ")...")

            response = client.chat.completions.create(

                model="z-ai/glm-5.1",

                messages=messages,

                timeout=30.0

            )

            return response

        except RateLimitError as e:

            if attempt < max_retries - 1:

                sleep_time = delay * (2 ** attempt) + random.uniform(0.5, 1.5)

                print("Rate Limit Hit (HTTP 429). Sleeping...")

                time.sleep(sleep_time)

            else:

                print("Critical Error: Retries exhausted for Rate Limit.")

                raise e

        except APIStatusError as e:

            print("NVIDIA API Server Error: " + str(e.message))

            raise e

        except Exception as e:

            print("Unexpected Internal System Error: " + str(e))

            raise e



if __name__ == "__main__":

    print("Starting Nvidia API Secure Connection Engine...")

    

    conversation_history = [

        {"role": "system", "content": "You are an expert AI assistant."},

        {"role": "user", "content": "Hello! Is the secure connection working properly now?"}

    ]

    try:

        api_result = safe_predict_nvidia(conversation_history)

        print("\nConnection Successful! Response received:")

        print(api_result.choices[0].message.content)

    except Exception as final_error:

        print("\nProcess terminated: " + str(final_error))

