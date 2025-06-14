#!/usr/bin/env python3

import os
import re
import yaml
import time
import requests
import mimetypes
from pathlib import Path
from PIL import Image
from google import genai
from google.genai import types

# Configuration
ARTICLES_DIR = "articles-folder"
COMPLETED_DIR = "checking-complete"
PLACEHOLDER_URL = "https://res.cloudinary.com/dbcpfy04c/image/upload/v1743184673/images_k6zam3.png"
MAX_FILES_PER_RUN = 50

# Ensure directories exist
os.makedirs(COMPLETED_DIR, exist_ok=True)

class APIKeyManager:
    """Manages multiple Gemini API keys with rotation"""
    
    def __init__(self):
        self.api_keys = self._load_api_keys()
        self.current_key_index = 0
        self.key_usage_count = {}
        self.max_requests_per_key = 100
        self.failed_keys = set()
        
        for key in self.api_keys:
            self.key_usage_count[key] = 0
    
    def _load_api_keys(self):
        """Load all available Gemini API keys from environment variables"""
        keys = []
        
        # Try to load GEMINI_API_KEY_1 through GEMINI_API_KEY_6
        for i in range(1, 7):
            key = os.environ.get(f"GEMINI_API_KEY_{i}")
            if key:
                keys.append(key)
                print(f"Loaded API key #{i}")
        
        # Also try the original GEMINI_API_KEY
        original_key = os.environ.get("GEMINI_API_KEY")
        if original_key and original_key not in keys:
            keys.append(original_key)
            print("Loaded original GEMINI_API_KEY")
        
        if not keys:
            raise ValueError("No Gemini API keys found in environment variables")
        
        print(f"Total API keys loaded: {len(keys)}")
        return keys
    
    def get_current_key(self):
        """Get the current active API key"""
        if not self.api_keys:
            raise ValueError("No API keys available")
        
        current_key = self.api_keys[self.current_key_index]
        
        if (current_key in self.failed_keys or 
            self.key_usage_count[current_key] >= self.max_requests_per_key):
            self._rotate_key()
            current_key = self.api_keys[self.current_key_index]
        
        return current_key
    
    def _rotate_key(self):
        """Rotate to the next available API key"""
        attempts = 0
        while attempts < len(self.api_keys):
            self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
            current_key = self.api_keys[self.current_key_index]
            
            if (current_key not in self.failed_keys and 
                self.key_usage_count[current_key] < self.max_requests_per_key):
                print(f"Rotated to API key #{self.current_key_index + 1}")
                return
            
            attempts += 1
        
        raise Exception("All API keys have been exhausted or failed")
    
    def increment_usage(self, key):
        """Increment usage count for a specific key"""
        if key in self.key_usage_count:
            self.key_usage_count[key] += 1
            print(f"API key usage: {self.key_usage_count[key]}/{self.max_requests_per_key}")
    
    def mark_key_as_failed(self, key, error_message):
        """Mark a key as failed"""
        self.failed_keys.add(key)
        print(f"Marked API key as failed: {error_message}")
        
        if key == self.api_keys[self.current_key_index]:
            try:
                self._rotate_key()
            except Exception as e:
                print(f"Failed to rotate key: {e}")

def save_binary_file(file_name, data):
    """Save binary data to file"""
    with open(file_name, "wb") as f:
        f.write(data)

def compress_image(image_path, quality=85):
    """Compress image and convert to WebP"""
    try:
        with Image.open(image_path) as img:
            webp_path = f"{os.path.splitext(image_path)[0]}.webp"
            img.save(webp_path, 'WEBP', quality=quality)
            os.remove(image_path)
            return webp_path
    except Exception as e:
        print(f"Image compression error: {e}")
        return image_path

def upload_to_cloudinary(file_path, resource_type="image"):
    """Upload file to Cloudinary"""
    url = f"https://api.cloudinary.com/v1_1/{os.environ['CLOUDINARY_CLOUD_NAME']}/{resource_type}/upload"
    payload = {
        'upload_preset': 'ml_default',
        'api_key': os.environ['CLOUDINARY_API_KEY']
    }
    try:
        with open(file_path, 'rb') as f:
            files = {'file': f}
            response = requests.post(url, data=payload, files=files)
        if response.status_code == 200:
            return response.json()['secure_url']
        print(f"Upload failed: {response.text}")
        return None
    except Exception as e:
        print(f"Upload error: {e}")
        return None

def generate_and_upload_image(title, api_key_manager):
    """Generate image using Gemini and upload to Cloudinary"""
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            current_key = api_key_manager.get_current_key()
            client = genai.Client(api_key=current_key)
            model = "gemini-2.0-flash-exp-image-generation"
            
            # Create a more descriptive prompt based on the title
            prompt = f"Create a realistic, professional blog header image for: {title}. Make it visually appealing and relevant to the topic."
            
            contents = [types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt)]
            )]
            
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=["image", "text"])
            )

            api_key_manager.increment_usage(current_key)

            if response.candidates and response.candidates[0].content.parts:
                inline_data = response.candidates[0].content.parts[0].inline_data
                file_ext = mimetypes.guess_extension(inline_data.mime_type)
                original_file = f"temp_image_{int(time.time())}{file_ext}"
                save_binary_file(original_file, inline_data.data)

                # Compress and convert to WebP
                final_file = compress_image(original_file)
                
                # Upload to Cloudinary
                uploaded_url = upload_to_cloudinary(final_file)
                
                # Clean up temporary file
                if os.path.exists(final_file):
                    os.remove(final_file)
                
                return uploaded_url
            return None
            
        except Exception as e:
            error_str = str(e).lower()
            
            if any(keyword in error_str for keyword in ['quota', 'resource_exhausted', 'limit']):
                print(f"Quota exhausted for current API key (attempt {attempt + 1}): {e}")
                api_key_manager.mark_key_as_failed(current_key, str(e))
                
                if attempt < max_retries - 1:
                    print("Retrying with next API key...")
                    time.sleep(2)
                    continue
            else:
                print(f"Image generation error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
            
            if attempt == max_retries - 1:
                print("Max retries reached for image generation")
                return None

def extract_frontmatter_and_content(file_content):
    """Extract YAML frontmatter and content from markdown file"""
    if not file_content.startswith('---'):
        return None, file_content
    
    # Find the end of frontmatter
    end_match = re.search(r'\n---\n', file_content[3:])
    if not end_match:
        return None, file_content
    
    frontmatter_end = end_match.end() + 3
    frontmatter_text = file_content[3:frontmatter_end-4]
    content = file_content[frontmatter_end:]
    
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        return frontmatter, content
    except yaml.YAMLError as e:
        print(f"Error parsing frontmatter: {e}")
        return None, file_content

def update_markdown_file(file_path, new_image_url):
    """Update the image URL in a markdown file"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        frontmatter, body_content = extract_frontmatter_and_content(content)
        
        if frontmatter is None:
            print(f"No valid frontmatter found in {file_path}")
            return False
        
        # Update the image URL
        frontmatter['image'] = new_image_url
        
        # Reconstruct the file
        new_content = "---\n"
        new_content += yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
        new_content += "---\n"
        new_content += body_content
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        return True
        
    except Exception as e:
        print(f"Error updating file {file_path}: {e}")
        return False

def get_title_from_markdown(file_path):
    """Extract title from markdown frontmatter"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        frontmatter, _ = extract_frontmatter_and_content(content)
        
        if frontmatter and 'title' in frontmatter:
            return frontmatter['title']
        else:
            # Fallback to filename
            return Path(file_path).stem.replace('-', ' ').title()
            
    except Exception as e:
        print(f"Error extracting title from {file_path}: {e}")
        return Path(file_path).stem.replace('-', ' ').title()

def has_placeholder_image(file_path):
    """Check if file has placeholder image"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return PLACEHOLDER_URL in content
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return False

def main():
    print("Starting image replacement process...")
    
    # Initialize API key manager
    try:
        api_key_manager = APIKeyManager()
    except Exception as e:
        print(f"Failed to initialize API key manager: {e}")
        return
    
    # Get all markdown files with placeholder images
    articles_path = Path(ARTICLES_DIR)
    if not articles_path.exists():
        print(f"Articles directory {ARTICLES_DIR} does not exist")
        return
    
    md_files = []
    for file_path in articles_path.glob("*.md"):
        if has_placeholder_image(file_path):
            md_files.append(file_path)
    
    if not md_files:
        print("No files with placeholder images found")
        return
    
    print(f"Found {len(md_files)} files with placeholder images")
    
    # Process up to MAX_FILES_PER_RUN files
    files_to_process = md_files[:MAX_FILES_PER_RUN]
    print(f"Processing {len(files_to_process)} files")
    
    processed_files = []
    
    for i, file_path in enumerate(files_to_process, 1):
        print(f"\n{'='*50}")
        print(f"Processing file {i}/{len(files_to_process)}: {file_path.name}")
        print(f"{'='*50}")
        
        try:
            # Extract title from the markdown file
            title = get_title_from_markdown(file_path)
            print(f"Title: {title}")
            
            # Generate new image
            print("Generating new image...")
            new_image_url = generate_and_upload_image(title, api_key_manager)
            
            if new_image_url:
                print(f"Generated image URL: {new_image_url}")
                
                # Update the markdown file
                if update_markdown_file(file_path, new_image_url):
                    print("Successfully updated markdown file")
                    
                    # Move file to completed directory
                    completed_path = Path(COMPLETED_DIR) / file_path.name
                    file_path.rename(completed_path)
                    print(f"Moved file to {completed_path}")
                    
                    processed_files.append(file_path.name)
                else:
                    print("Failed to update markdown file")
            else:
                print("Failed to generate image, skipping file")
            
        except Exception as e:
            print(f"Error processing file {file_path}: {e}")
            continue
        
        # Add delay between requests
        if i < len(files_to_process):
            print("Waiting 5 seconds before next file...")
            time.sleep(5)
    
    # Print final status
    print(f"\n{'='*50}")
    print("PROCESSING COMPLETE")
    print(f"{'='*50}")
    print(f"Successfully processed {len(processed_files)} files:")
    for filename in processed_files:
        print(f"  - {filename}")
    
    # Print API key usage status
    print(f"\nAPI Key Usage Status:")
    for i, key in enumerate(api_key_manager.api_keys, 1):
        usage = api_key_manager.key_usage_count[key]
        failed = key in api_key_manager.failed_keys
        active = i - 1 == api_key_manager.current_key_index
        print(f"  Key {i}: {usage}/{api_key_manager.max_requests_per_key} requests"
              f"{' (FAILED)' if failed else ''}"
              f"{' (ACTIVE)' if active else ''}")

if __name__ == "__main__":
    main()
