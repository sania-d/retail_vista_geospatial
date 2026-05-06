import os
from google.adk.deploy import deploy_agent
from agents.main_agent import root_agent

# Set GCP Project & Region Environment variables
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/usr/local/google/home/saniadawesar/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "sandaw-project-2121"
os.environ["GOOGLE_CLOUD_REGION"] = "us-central1"

print("=========================================================")
print("🚀 Deploying Retail Vista V5 natively to Vertex AI...")
print("=========================================================")

try:
    deployed_agent = deploy_agent(
        agent=root_agent,
        project="sandaw-project-2121",
        location="us-central1"
    )
    print("\n🎉 SUCCESS! Native ADK Agent successfully deployed!")
    print(f"Agent Resource Name: {deployed_agent.resource_name}")
    print("=========================================================")
except Exception as e:
    print("\n❌ Deployment failed:", e)
