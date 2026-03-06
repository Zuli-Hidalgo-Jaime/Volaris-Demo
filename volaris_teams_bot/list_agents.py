from dotenv import load_dotenv
load_dotenv()

import os
from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient

client = AgentsClient(
    endpoint=os.environ["PROJECT_ENDPOINT"],
    credential=DefaultAzureCredential(),
)

for a in client.list_agents():
    print(f"{a.name} -> {a.id}")