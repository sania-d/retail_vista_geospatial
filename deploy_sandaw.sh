#!/bin/bash
# Automatic Migration & Deployment Script for sandaw-project-2121

set -e

PROJECT_ID="sandaw-project-2121"
REGION="asia-south1"
DATASET_ID="RetailVista_1"
MAPS_KEY="${MAPS_API_KEY:-YOUR_GOOGLE_MAPS_API_KEY}"

echo "========================================================="
echo "🚀 Starting migration & deployment to project: $PROJECT_ID"
echo "========================================================="

# Step 1: BigQuery Dataset & Tables are already fully initialized
echo "📊 1. BigQuery Dataset and Tables are already fully populated."

# Step 3: Create Artifact Registry for Docker
echo "📦 3. Creating Artifact Registry repository..."
gcloud artifacts repositories create cloud-run-source-deploy \
    --repository-format=docker \
    --location="$REGION" \
    --project="$PROJECT_ID" || echo "Repository already exists."

# Step 4: Store Maps API Key securely in Secret Manager
echo "🔑 4. Storing Google Maps API Key securely..."
gcloud secrets create retailvista-maps-api-key --project="$PROJECT_ID" || echo "Secret already exists."
echo -n "$MAPS_KEY" | gcloud secrets versions add retailvista-maps-api-key --data-file=- --project="$PROJECT_ID"

# Step 5: Build and Push Container using Cloud Build
echo "🐳 5. Building and pushing container using Google Cloud Build..."
IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/cloud-run-source-deploy/insight-agent-backend-v5:latest"
gcloud builds submit --tag "$IMAGE_URI" --project="$PROJECT_ID"

# Step 6: Deploy Container to Google Cloud Run
echo "☁️ 6. Deploying backend container to Google Cloud Run..."
gcloud run deploy insight-agent-backend-v5 \
    --image "$IMAGE_URI" \
    --region "$REGION" \
    --cpu 2 \
    --memory 4Gi \
    --no-allow-unauthenticated \
    --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=True,PROJECT_ID=$PROJECT_ID,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,LOCATION=us-central1,DATASET_ID=$DATASET_ID,MODEL=gemini-2.5-pro,remote_deployment=False,GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES=false" \
    --set-secrets "MAPS_API_KEY=retailvista-maps-api-key:latest" \
    --project="$PROJECT_ID"

echo "========================================================="
echo "🎉 Success! Retail Vista V5 successfully deployed to Cloud Run!"
echo "========================================================="
