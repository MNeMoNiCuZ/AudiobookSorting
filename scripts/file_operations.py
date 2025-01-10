from pathlib import Path
import shutil
import logging
import re
from typing import Dict, Optional

class FileOperations:
    def __init__(self, output_dir: str = 'output', copy_mode: bool = True):
        self.output_dir = Path(output_dir)
        self.copy_mode = copy_mode
        self.logger = logging.getLogger(__name__)
        
    def sanitize_path_component(self, component: str) -> str:
        """Sanitize a path component to be safe for filesystem"""
        # Replace invalid characters with underscore
        sanitized = re.sub(r'[<>:"/\\|?*]', '_', component)
        # Remove leading/trailing spaces and dots
        sanitized = sanitized.strip('. ')
        return sanitized or 'Unknown'
        
    def apply_entry(self, entry: Dict) -> Optional[str]:
        """Apply file organization for an entry"""
        try:
            # Sanitize path components
            author = self.sanitize_path_component(entry.get('author', 'Unknown Author'))
            series = self.sanitize_path_component(entry.get('series', ''))
            title = self.sanitize_path_component(entry.get('title', 'Unknown Title'))
            
            # Handle series index
            series_index = entry.get('series_index', '')
            if series_index:
                try:
                    series_index = f"{int(series_index):02d}"
                except ValueError:
                    self.logger.warning(f"Invalid series index: {series_index}")
                    series_index = ''
            
            # Create target directory name
            if series and series_index:
                book_dir = f"{series} {series_index} - {title}"
            elif series:
                book_dir = f"{series} - {title}"
            else:
                book_dir = title
                
            # Create full target path
            target_path = self.output_dir / author / self.sanitize_path_component(book_dir)
            
            # Get source path and its parent directory
            source_path = Path(entry['full_audio_path'])
            source_dir = source_path.parent
            
            if not source_dir.exists():
                self.logger.error(f"Source directory does not exist: {source_dir}")
                return None
            
            # Create target directory
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.mkdir(exist_ok=True)
            
            # Process all files in the source directory
            for file_path in source_dir.iterdir():
                if file_path.is_file():  # Only process files, not subdirectories
                    try:
                        if self.copy_mode:
                            self.logger.info(f"Copying {file_path} to {target_path}")
                            shutil.copy2(file_path, target_path / file_path.name)
                        else:
                            self.logger.info(f"Moving {file_path} to {target_path}")
                            shutil.move(str(file_path), str(target_path / file_path.name))
                    except Exception as e:
                        self.logger.error(f"Failed to process file {file_path}: {str(e)}")
                        continue
                    
            return str(target_path)
            
        except Exception as e:
            self.logger.error(f"Error applying entry: {str(e)}")
            return None 