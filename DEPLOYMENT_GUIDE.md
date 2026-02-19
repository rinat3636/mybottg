# Deployment Guide: ComfyUI Migration

This guide explains how to deploy the updated Telegram bot with ComfyUI integration on Railway.

## Prerequisites

- ✅ Railway account with active project
- ✅ PostgreSQL database (provided by Railway)
- ✅ Redis instance (provided by Railway)
- ✅ RunPod account with ComfyUI instance
- ✅ Telegram bot token from @BotFather

## Step 1: Prepare RunPod ComfyUI Instance

### 1.1 Verify ComfyUI is Running

1. Go to https://runpod.io and log in
2. Open your Pod: `resident_salmon_mongoose`
3. Click "Connect" → "HTTP Service (Port 8188)"
4. You should see ComfyUI interface loading

### 1.2 Get ComfyUI URL

Your ComfyUI URL is:
```
https://o2zqe9gq92hyf2-8188.proxy.runpod.net
```

**Important:** This URL changes if you restart the Pod. Save it for Railway configuration.

### 1.3 Verify SDXL Model Installation

1. In ComfyUI interface, add a "Load Checkpoint" node
2. Check the dropdown for available models
3. Note the exact filename (e.g., `sd_xl_base_1.0.safetensors`)
4. Update `workflows/sdxl_workflow.json` with the correct filename:

```json
{
  "4": {
    "inputs": {
      "ckpt_name": "YOUR_MODEL_NAME.safetensors"
    },
    "class_type": "CheckpointLoaderSimple"
  }
}
```

### 1.4 Install LivePortrait (Optional, for Video Generation)

If you want video generation feature:

```bash
# SSH into your RunPod instance
ssh root@your-runpod-ip

# Navigate to ComfyUI custom nodes
cd /workspace/ComfyUI/custom_nodes

# Clone LivePortrait node
git clone https://github.com/kijai/ComfyUI-LivePortraitKJ.git

# Install dependencies
cd ComfyUI-LivePortraitKJ
pip install -r requirements.txt

# Download models (follow the node's README)
# Models usually go in: /workspace/ComfyUI/models/liveportrait/

# Restart ComfyUI
pkill -f "python.*main.py"
cd /workspace/ComfyUI
python main.py --listen 0.0.0.0 --port 8188
```

Then update `workflows/liveportrait_workflow.json` based on your actual ComfyUI setup.

## Step 2: Update Railway Environment Variables

### 2.1 Access Railway Dashboard

1. Go to https://railway.app
2. Select your project
3. Click on your service
4. Go to "Variables" tab

### 2.2 Remove Old Variables

Delete these variables (no longer needed):
- `REPLICATE_API_TOKEN`

### 2.3 Add New ComfyUI Variables

Add these new environment variables:

```bash
COMFYUI_API_URL=https://o2zqe9gq92hyf2-8188.proxy.runpod.net
COMFYUI_API_PORT=8188
COMFYUI_POLL_INTERVAL=3
```

Optional (only if you enabled authentication on ComfyUI):
```bash
COMFYUI_API_KEY=your_api_key_here
```

### 2.3 Verify Existing Variables

Make sure these are still set:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_URL`
- `TELEGRAM_WEBHOOK_SECRET`
- `DATABASE_URL` (or `POSTGRES_URL` - Railway sets this automatically)
- `REDIS_URL` (Railway sets this automatically)
- `YOOKASSA_SHOP_ID`
- `YOOKASSA_SECRET_KEY`
- `YOOKASSA_WEBHOOK_SECRET`
- `ADMIN_IDS`

## Step 3: Deploy Updated Code to Railway

### 3.1 Connect to GitHub Repository

If not already connected:
1. In Railway dashboard, click "Settings"
2. Connect to your GitHub repository: `rinat3636/mybottg`
3. Set branch to `main` (or your deployment branch)

### 3.2 Push Updated Code

```bash
# Navigate to your local project
cd /path/to/mybottg

# Add all changes
git add .

# Commit changes
git commit -m "Migrate from Replicate to ComfyUI"

# Push to GitHub
git push origin main
```

Railway will automatically detect the changes and redeploy.

### 3.3 Monitor Deployment

1. In Railway dashboard, go to "Deployments" tab
2. Watch the build logs
3. Look for:
   - ✅ Dependencies installed successfully
   - ✅ Application started
   - ✅ ComfyUI connection established

## Step 4: Test the Bot

### 4.1 Basic Functionality Test

1. Open Telegram and find your bot
2. Send `/start` command
3. Click "Генерация" button
4. Send a text prompt (e.g., "beautiful sunset over mountains")
5. Wait for the image to be generated

### 4.2 Image Editing Test

1. Click "Генерация" button
2. Upload a photo
3. Send editing instructions (e.g., "make it look like a painting")
4. Wait for the edited image

### 4.3 Video Generation Test (if LivePortrait is installed)

1. Click "Видео" button
2. Upload a photo with a face
3. Select duration (5 or 10 seconds)
4. Wait for the video to be generated

## Step 5: Troubleshooting

### Error: "ComfyUI is not responding"

**Cause:** RunPod Pod is stopped or ComfyUI is not running

**Solution:**
1. Go to RunPod dashboard
2. Start your Pod if it's stopped
3. Wait 2-3 minutes for ComfyUI to fully start
4. Try generation again

### Error: "Workflow template not found"

**Cause:** Workflow JSON files are missing or incorrectly configured

**Solution:**
1. Check that `workflows/sdxl_workflow.json` exists
2. Verify the model name in the workflow matches your installed model
3. Redeploy the code

### Error: "No output file found"

**Cause:** ComfyUI workflow is not producing output

**Solution:**
1. Test the workflow directly in ComfyUI UI
2. Make sure the workflow has a "SaveImage" node
3. Check ComfyUI logs for errors

### Error: "Generation timed out"

**Cause:** Generation is taking too long (>200 seconds)

**Solution:**
1. Increase `GENERATION_TIMEOUT` in Railway variables
2. Check GPU utilization on RunPod
3. Simplify the prompt or reduce image resolution

### Error: "No face detected" (video generation)

**Cause:** LivePortrait couldn't find a face in the uploaded photo

**Solution:**
1. Make sure the photo has a clear, visible face
2. Face should be well-lit and not obscured
3. Try a different photo

## Step 6: Cost Optimization

### RunPod Cost Management

Your current Pod costs **$0.27/hr** (~$194/month if running 24/7).

**Optimization strategies:**

1. **Stop Pod when not in use:**
   - Manually stop the Pod in RunPod dashboard
   - Start it only when needed
   - Update `COMFYUI_API_URL` in Railway after restart (URL changes)

2. **Use Serverless (future upgrade):**
   - RunPod Serverless charges only for actual generation time
   - Requires code changes to support serverless API
   - More cost-effective for low-medium traffic

3. **Auto-scaling (advanced):**
   - Set up a script to start/stop Pod based on queue size
   - Use Railway cron jobs to manage Pod lifecycle

### Railway Cost

Railway charges based on:
- Compute time
- Database storage
- Redis storage
- Bandwidth

**Current usage:** ~$0.52/day (~$15.60/month)

## Step 7: Monitoring and Maintenance

### Check Application Logs

```bash
# In Railway dashboard
1. Go to your service
2. Click "Logs" tab
3. Look for errors or warnings
```

### Monitor Queue Status

The bot logs queue status in Railway logs:
- Task enqueued
- Task processing
- Task completed/failed

### Monitor ComfyUI Health

Check ComfyUI status:
```bash
curl https://o2zqe9gq92hyf2-8188.proxy.runpod.net/system_stats
```

Should return JSON with GPU stats.

## Step 8: Updating Workflows

### When to Update Workflows

- You install a new model
- You want to change generation parameters
- You add custom nodes

### How to Update Workflows

1. Create/modify workflow in ComfyUI UI
2. Click "Save (API Format)"
3. Replace `workflows/sdxl_workflow.json` or `workflows/liveportrait_workflow.json`
4. Commit and push to GitHub
5. Railway will auto-deploy

## Step 9: Backup and Rollback

### Backup Current Working Version

```bash
# Create a backup branch
git checkout -b backup-replicate-version
git push origin backup-replicate-version
```

### Rollback if Needed

```bash
# Revert to previous version
git revert HEAD
git push origin main
```

Or in Railway:
1. Go to "Deployments" tab
2. Find previous successful deployment
3. Click "Redeploy"

## Step 10: Next Steps

### Recommended Improvements

1. **Add image-to-image workflow:**
   - Create proper img2img workflow in ComfyUI
   - Update `comfy_client.py` to support it

2. **Optimize VRAM usage:**
   - Enable model offloading in ComfyUI
   - Use smaller VAE for faster processing

3. **Add more models:**
   - Install additional SDXL models
   - Create model selection in bot UI

4. **Implement caching:**
   - Cache workflow JSONs in memory
   - Reduce file I/O overhead

5. **Add metrics:**
   - Track generation success rate
   - Monitor average generation time
   - Alert on failures

## Support

If you encounter issues:

1. Check Railway logs for errors
2. Check RunPod Pod status
3. Test ComfyUI directly in browser
4. Review `workflows/README.md` for workflow customization

## Summary Checklist

- [ ] RunPod Pod is running
- [ ] ComfyUI is accessible at the URL
- [ ] SDXL model is installed and workflow is updated
- [ ] LivePortrait is installed (if using video generation)
- [ ] Railway environment variables are updated
- [ ] Code is pushed to GitHub
- [ ] Railway deployment is successful
- [ ] Bot responds to `/start` command
- [ ] Image generation works
- [ ] Image editing works (if applicable)
- [ ] Video generation works (if applicable)
- [ ] Credits are deducted correctly
- [ ] Refunds work on errors
- [ ] Queue system is functioning

**Congratulations!** Your bot is now running with ComfyUI instead of Replicate. 🎉
