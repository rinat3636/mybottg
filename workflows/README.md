# ComfyUI Workflow Templates

This directory contains workflow JSON files for ComfyUI generation.

## Files

- `sdxl_workflow.json` - SDXL text-to-image workflow
- `liveportrait_workflow.json` - LivePortrait photo animation workflow

## Customization Instructions

These workflow templates are **generic examples** and must be customized based on your actual ComfyUI setup.

### How to Get Your Workflow JSON

1. **Open ComfyUI** in your browser (e.g., `http://your-runpod-ip:8188`)

2. **Load or create your workflow** in the ComfyUI interface:
   - For SDXL: Set up a basic text-to-image workflow
   - For LivePortrait: Set up a photo animation workflow

3. **Export the workflow**:
   - Click "Save (API Format)" button in ComfyUI
   - This saves the workflow in API-compatible JSON format
   - Save it to this directory

4. **Update the workflow files**:
   - Replace `sdxl_workflow.json` with your SDXL workflow
   - Replace `liveportrait_workflow.json` with your LivePortrait workflow

### Important Notes

#### SDXL Workflow Requirements

Your SDXL workflow must include these nodes:

1. **CheckpointLoaderSimple** - Load SDXL model
   - Update `ckpt_name` to match your model file name
   - Example: `"sd_xl_base_1.0.safetensors"` or `"sdxl_model.safetensors"`

2. **CLIPTextEncode (Positive)** - For the main prompt
   - The code will update the `text` input dynamically
   - Add `"_meta": {"title": "CLIP Text Encode (Positive Prompt)"}` to identify it

3. **CLIPTextEncode (Negative)** - For negative prompt
   - Add `"_meta": {"title": "CLIP Text Encode (Negative Prompt)"}` to identify it

4. **KSampler** - Sampling settings
   - The code will update: `seed`, `steps`, `cfg`

5. **EmptyLatentImage** - Image dimensions
   - The code will update: `width`, `height`

6. **SaveImage** - Save the output
   - Must be present to generate output files

#### LivePortrait Workflow Requirements

Your LivePortrait workflow must include:

1. **LoadImage** - Load the input photo
   - The code will handle image upload

2. **LivePortraitProcess** - Main animation node
   - The code will update `duration_frames` based on requested duration

3. **VHS_VideoCombine** or similar - Video output node
   - Must save as MP4 format
   - Recommended settings: 30fps, H.264 codec

### Node ID Mapping

The node IDs (e.g., "3", "4", "5") in the workflow JSON are important:

- They define the connections between nodes
- When you export from ComfyUI, these IDs are automatically assigned
- The Python code searches by `class_type` and `_meta.title`, not by node ID
- So you don't need to manually adjust IDs

### Testing Your Workflow

Before using in production:

1. **Test in ComfyUI UI** - Make sure the workflow runs successfully
2. **Test via API** - Use the ComfyUI API to submit the workflow
3. **Check outputs** - Verify that output files are generated correctly

### Example: Getting SDXL Workflow

```bash
# 1. SSH into your RunPod instance
ssh root@your-runpod-ip

# 2. Navigate to ComfyUI directory
cd /workspace/ComfyUI

# 3. Check installed models
ls models/checkpoints/

# 4. Update the workflow JSON with the correct model name
# Edit: workflows/sdxl_workflow.json
# Change: "ckpt_name": "YOUR_MODEL_NAME.safetensors"
```

### Example: Installing LivePortrait

If LivePortrait is not installed on your RunPod instance:

```bash
# SSH into RunPod
ssh root@your-runpod-ip

# Navigate to ComfyUI custom nodes
cd /workspace/ComfyUI/custom_nodes

# Clone LivePortrait node
git clone https://github.com/kijai/ComfyUI-LivePortraitKJ.git

# Install dependencies
cd ComfyUI-LivePortraitKJ
pip install -r requirements.txt

# Download models (follow the node's README)
# Usually models go in: ComfyUI/models/liveportrait/

# Restart ComfyUI
# Then create your workflow in the UI and export it
```

## Troubleshooting

### "Workflow template not found" error

- Make sure the JSON files exist in the `workflows/` directory
- Check file permissions: `chmod 644 workflows/*.json`

### "No output file found" error

- Check that your workflow has a SaveImage or Video output node
- Verify the output node is connected properly
- Check ComfyUI logs for generation errors

### "Invalid workflow" error

- Validate your JSON syntax: `python -m json.tool workflow.json`
- Make sure all node connections are valid
- Test the workflow in ComfyUI UI first

### Model not found

- Update `ckpt_name` in the workflow to match your actual model file
- Check model path: `ls /workspace/ComfyUI/models/checkpoints/`
- Make sure the model is fully downloaded

## Additional Resources

- [ComfyUI Documentation](https://github.com/comfyanonymous/ComfyUI)
- [ComfyUI API Documentation](https://github.com/comfyanonymous/ComfyUI/wiki/API)
- [LivePortrait Node](https://github.com/kijai/ComfyUI-LivePortraitKJ)
- [SDXL on ComfyUI](https://comfyanonymous.github.io/ComfyUI_examples/sdxl/)
