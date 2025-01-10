import json
import logging
from typing import Dict, Optional
from pathlib import Path
from api_engine import APIEngine
from configparser import ConfigParser

class LLMQueryClient:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # Load configuration
        config = ConfigParser()
        config.read('config.ini')
        
        self.engine = config.get('API', 'engine_default', fallback='groq')
        self.temperature = config.getfloat('Settings', 'temperature_default', fallback=0.1)
        self.max_tokens = config.getint('Settings', 'max_tokens_default', fallback=500)
        self.model = config.get('Models', f'{self.engine}_default', fallback=None)
        
        self.api_engine = APIEngine(engine=self.engine)

    def query_metadata(self, verified_metadata: Dict, file_structure: Optional[Dict] = None) -> Optional[Dict]:
        """Query LLM to analyze and complete book metadata"""
        self.logger.info("Preparing LLM query with metadata")
        
        # Construct the system message
        system_message = """You are an expert librarian with vast knowledge of books, series, and authors across all genres. You have decades of experience organizing library collections and maintaining book metadata.

Your task is to analyze book information and use your EXTENSIVE KNOWLEDGE to:
1. Identify the complete series information
2. Determine correct book order
3. Clean up and standardize titles
4. Fill in missing author information

IMPORTANT: 
- Do not just repeat the provided data
- Never replace pseudonyms with real names
- Keep author names exactly as provided
- Use your knowledge of:
  - Book series across all genres
  - Common author naming patterns
  - Standard series structures
  - Publishing conventions
  - Literary works and their organization

Return ONLY a JSON object with your expert analysis."""

        # Build the user prompt
        prompt = "As an expert librarian, analyze this book's information and apply your extensive knowledge.\n\n"
        
        # Add file structure information first for context
        if file_structure and file_structure.get('files'):
            prompt += "DIRECTORY CONTENTS:\n"
            if file_structure.get('path'):
                prompt += f"Path: {file_structure['path']}\n"
            prompt += "Files:\n"
            for file in file_structure['files']:
                prompt += f"- {file}\n"
            prompt += "\n"
        
        # Add current metadata
        prompt += "CURRENT METADATA:\n"
        for key, value in verified_metadata.items():
            prompt += f"{key}: {value}\n"
        prompt += "\n"
        
        prompt += "Based on the directory contents and current metadata, please provide complete book information."

        # Prepare API prompt
        api_prompt = {
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,  # Lower temperature for more consistent output
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"}  # Explicitly request JSON
        }

        try:
            self.logger.debug("Sending query to LLM")
            response = self.api_engine.call_api(
                api_prompt, 
                model=self.model
            )
            
            # Validate response
            if not response:
                self.logger.error("Empty response from LLM")
                return None
            
            # Ensure response is valid JSON
            try:
                # Remove any leading/trailing non-JSON content
                json_start = response.find('{')
                json_end = response.rfind('}') + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response[json_start:json_end]
                    result = json.loads(json_str)
                    
                    # Validate required fields
                    required_fields = {'title', 'author', 'series', 'series_index'}
                    if not all(field in result for field in required_fields):
                        self.logger.error("Missing required fields in LLM response")
                        return None
                    
                    self.logger.info("Successfully parsed LLM response")
                    return result
                else:
                    self.logger.error("No valid JSON object found in response")
                    return None
                
            except json.JSONDecodeError as e:
                self.logger.error(f"Failed to parse LLM response as JSON: {e}")
                self.logger.debug(f"Raw response: {response}")
                return None
            
        except Exception as e:
            self.logger.error(f"Error during LLM query: {str(e)}")
            return None

        return None 