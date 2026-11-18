import json
import os
import sys
import time
import argparse
from loguru import logger

# Ensure we can import app modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.config import config
from app.services.llm import _generate_response
from app.services.task import start as start_task
from app.services.upload_post import cross_post_video
from app.utils.utils import get_uuid

STATE_FILE = "storage/batch_state.json"
PACING_SECONDS = 300  # 5 minutes

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load state file: {e}")
        return {}

def save_state(state):
    os.makedirs("storage", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def generate_topics(main_topic, batch_size=50):
    prompt = f"""
# Role: YouTube Shorts Strategist

## Goal:
Generate {batch_size} highly controversial, engaging, and unique YouTube Shorts video subjects based on the main topic: '{main_topic}'.
These topics must be designed to maximize views, provoke discussion, and capture attention immediately.

## Constraints:
1. Return EXACTLY {batch_size} subjects.
2. Return ONLY a valid JSON array of strings. Do not include markdown code blocks, titles, or any other text.
3. Each subject should be short and catchy (max 10 words).

## Example Output:
[
  "The dark truth about...",
  "Why you should never...",
  "The biggest lie you've been told about..."
]
"""
    logger.info(f"Generating {batch_size} controversial topics for: {main_topic}")
    response = _generate_response(prompt)
    if response.startswith("Error: "):
        logger.error(f"Failed to generate topics: {response}")
        return []
        
    # Clean markdown if present
    cleaned = response.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    
    try:
        topics = json.loads(cleaned)
        if isinstance(topics, list) and all(isinstance(t, str) for t in topics):
            return topics
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {e}\nResponse was:\n{cleaned}")
    
    return []

def main():
    logger.add("storage/logs/batch_worker.log", rotation="10 MB")
    logger.info("Batch worker started.")
    
    state = load_state()
    if state.get("status") != "running":
        logger.info("Status is not 'running'. Exiting.")
        return

    main_topic = state.get("main_topic")
    if not main_topic:
        logger.error("No main_topic found in state. Exiting.")
        return

    pending_topics = state.get("pending_topics", [])
    completed_topics = state.get("completed_topics", [])
    batch_size = state.get("batch_size", 50)

    if not pending_topics and not completed_topics:
        # First time running, generate topics
        state["status_message"] = f"Generating {batch_size} controversial topics..."
        save_state(state)
        
        pending_topics = generate_topics(main_topic, batch_size)
        if not pending_topics:
            state["status"] = "failed"
            state["status_message"] = "Failed to generate topics."
            save_state(state)
            return
            
        state["pending_topics"] = pending_topics
        save_state(state)

    while pending_topics:
        # Re-check state to see if user paused/stopped
        state = load_state()
        if state.get("status") != "running":
            logger.info("Worker stopped by user.")
            break

        current_topic = pending_topics[0]
        state["status_message"] = f"Generating video for: {current_topic}"
        state["current_topic"] = current_topic
        save_state(state)
        
        logger.info(f"Starting generation for topic: {current_topic}")
        
        task_id = get_uuid()
        try:
            # Build basic params for task
            from app.models.schema import VideoParams
            from cli import build_video_params
            import argparse
            
            # Using defaults similar to CLI, but customized for this topic
            args = argparse.Namespace(
                video_subject=current_topic,
                video_script="",
                video_terms=None,
                video_source="pexels",
                video_materials="",
                video_count=1,
                video_aspect="9:16",
                voice_name="en-US-ChristopherNeural-Male", # using a standard edge-tts voice
                subtitle_enabled=True,
                video_language=None,
                paragraph_number=1,
                video_script_prompt="",
                custom_system_prompt="",
                video_concat_mode=None,
                video_transition_mode=None,
                video_clip_duration=None,
                match_materials_to_script=None,
                n_threads=None,
                voice_volume=None,
                voice_rate=None,
                custom_audio_file=None,
                bgm_type=None,
                bgm_file=None,
                bgm_volume=None,
                sonilo_bgm_prompt=None,
                font_name=None,
                subtitle_position=None,
                custom_position=None,
                text_fore_color=None,
                font_size=None,
                stroke_color=None,
                stroke_width=None,
                rounded_subtitle_background=None,
                subtitle_background_enabled=None,
                subtitle_background_color=None
            )
            
            params = build_video_params(args)
            result = start_task(task_id, params, stop_at="video", allow_server_file_input=True)
            
            if result and result.get("state") == 1: # TASK_STATE_COMPLETE
                logger.info(f"Video generated successfully for: {current_topic}")
                
                # Retrieve final video path
                video_files = result.get("videos", [])
                if video_files:
                    video_path = video_files[0]
                    
                    # Upload to YouTube
                    platforms = config.app.get("upload_post_platforms", ["youtube"])
                    if not platforms:
                        platforms = ["youtube"]
                        
                    logger.info(f"Uploading video {video_path} to {platforms}...")
                    state["status_message"] = f"Uploading video: {current_topic}"
                    save_state(state)
                    
                    youtube_extra = {
                        "youtube_title": current_topic,
                        "youtube_description": f"{current_topic} #shorts #viral",
                        "tags": ["shorts", "viral"],
                        "privacyStatus": config.app.get("upload_post_youtube_privacy_status", "public")
                    }
                    
                    upload_res = cross_post_video(
                        video_path=video_path,
                        title=current_topic,
                        platforms=platforms,
                        youtube_extra=youtube_extra
                    )
                    
                    if upload_res.get("success"):
                        logger.info("Upload successful.")
                    else:
                        logger.error(f"Upload failed: {upload_res.get('error')}")
            else:
                logger.error(f"Task failed for topic: {current_topic}. Error: {result.get('error')}")
                
        except Exception as e:
            logger.exception(f"Exception during video generation for {current_topic}: {e}")

        # Move to completed
        pending_topics.pop(0)
        completed_topics.append(current_topic)
        state["pending_topics"] = pending_topics
        state["completed_topics"] = completed_topics
        state["current_topic"] = ""
        
        if pending_topics:
            state["status_message"] = f"Waiting {PACING_SECONDS} seconds before next video..."
            save_state(state)
            logger.info(f"Pacing: waiting {PACING_SECONDS} seconds.")
            time.sleep(PACING_SECONDS)
        else:
            state["status"] = "completed"
            state["status_message"] = "Batch processing completed successfully!"
            save_state(state)
            logger.info("Batch processing completed.")

if __name__ == "__main__":
    main()
