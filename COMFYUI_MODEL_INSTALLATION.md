# ComfyUI Model Installation Guide for RunPod

This guide provides detailed instructions for installing the necessary models and custom nodes on your RunPod ComfyUI instance to enable all features of the bot.

## Prerequisites

- A running RunPod instance with ComfyUI.
- SSH access to your RunPod instance.

## Installation Steps

### Step 1: SSH into Your RunPod Instance

First, connect to your RunPod instance via SSH. You can find the SSH command in your RunPod console.

```bash
ssh root@your-runpod-ip
```

### Step 2: Install SDXL Refiner

The SDXL Refiner model improves the quality of generated images. It is highly recommended for production use.

1.  **Navigate to the checkpoints directory**:

    ```bash
    cd /workspace/ComfyUI/models/checkpoints
    ```

2.  **Download the SDXL Refiner model**:

    ```bash
    wget https://huggingface.co/stabilityai/stable-diffusion-xl-refiner-1.0/resolve/main/sd_xl_refiner_1.0.safetensors
    ```

### Step 3: Install IP-Adapter and InsightFace

These models are crucial for preserving face identity in image-to-image and photo animation tasks.

1.  **Install the IP-Adapter custom node**:

    ```bash
    cd /workspace/ComfyUI/custom_nodes
    git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus.git
    ```

2.  **Install the required Python packages**:

    ```bash
    cd ComfyUI_IPAdapter_plus
    pip install -r requirements.txt
    ```

3.  **Download the IP-Adapter models**:

    ```bash
    cd /workspace/ComfyUI/models/ipadapter
    wget https://huggingface.co/h94/IP-Adapter/resolve/main/models/ip-adapter-faceid-plusv2_sdxl.bin
    wget https://huggingface.co/h94/IP-Adapter/resolve/main/models/ip-adapter-faceid-plus_sdxl_lora.safetensors
    ```

4.  **Download the InsightFace model**:

    ```bash
    cd /workspace/ComfyUI/models/insightface
    wget https://github.com/deepinsight/insightface/releases/download/v0.7/antelopev2.zip
    unzip antelopev2.zip
    ```

### Step 4: Install LivePortrait

LivePortrait is used for the photo animation feature.

1.  **Install the LivePortrait custom node**:

    ```bash
    cd /workspace/ComfyUI/custom_nodes
    git clone https://github.com/kijai/ComfyUI-LivePortraitKJ.git
    ```

2.  **Install the required Python packages**:

    ```bash
    cd ComfyUI-LivePortraitKJ
    pip install -r requirements.txt
    ```

3.  **Download the LivePortrait models**:

    ```bash
    cd /workspace/ComfyUI/models/liveportrait
    wget https://huggingface.co/Kijai/LivePortrait_v1.0/resolve/main/liveportrait_v1.0.safetensors
    ```

### Step 5: Restart ComfyUI

After installing all the models and custom nodes, you need to restart ComfyUI for the changes to take effect.

```bash
# Find the ComfyUI process ID
pgrep -f "python.*main.py"

# Kill the process
kill <process_id>

# Restart ComfyUI in the background
cd /workspace/ComfyUI
nohup python main.py --listen 0.0.0.0 --port 8188 > /workspace/comfyui.log 2>&1 &
```

## Verifying the Installation

1.  Open your ComfyUI instance in a web browser.
2.  Double-click to open the node search menu.
3.  Search for the following nodes to ensure they are available:
    -   `IPAdapter`
    -   `LivePortrait`
4.  In the `Load Checkpoint` node, you should see `sd_xl_refiner_1.0.safetensors` in the dropdown menu.

Once you have verified the installation, you can proceed with deploying the updated bot code.
