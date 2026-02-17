# gpt_parser.py

import os
from openai import OpenAI

from dotenv import load_dotenv


load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def parse_with_gpt(prompt,role="user",model="gpt-5"):
    
    messages = [
        {
            "role": role, 
            "content": prompt
        }
    ]
    
    
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"}
    )

    print(response.choices[0].message.content)
    
    return response.choices[0].message.content
