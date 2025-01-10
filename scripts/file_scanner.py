import os
from pathlib import Path
from typing import Dict, List, Optional
import logging

class FileScanner:
    def __init__(self, input_dir: str):
        self.input_dir = Path(input_dir)
        self.supported_audio = ('.m4b', '.mp3')
        self.supported_images = ('.jpg', '.jpeg', '.png')
        self.logger = logging.getLogger(__name__)

    def scan_directory(self) -> List[Dict]:
        """Scans the input directory and returns a list of entries"""
        file_groups = {}  # Dictionary to group files by their parent directory
        
        for root, _, files in os.walk(self.input_dir):
            audio_files = [f for f in files if f.lower().endswith(self.supported_audio)]
            if audio_files:
                root_path = Path(root)
                
                # Skip files directly in input directory
                if root_path == self.input_dir:
                    continue
                    
                # Use parent directory as group key, but ensure it's not the input directory
                group_key = root_path.parent
                if group_key == self.input_dir:
                    group_key = root_path
                
                if group_key not in file_groups:
                    file_groups[group_key] = []
                
                for audio_file in audio_files:
                    file_groups[group_key].append({
                        'full_audio_path': str(root_path / audio_file),
                        'folder_structure': self._get_folder_structure(root, audio_files)
                    })
        
        # Convert groups to entries
        entries = []
        for files in file_groups.values():
            if files:
                # Use the first file as the primary entry
                primary = files[0]
                if len(files) > 1:
                    # If multiple files, combine their paths
                    primary['additional_files'] = [f['full_audio_path'] for f in files[1:]]
                entries.append(primary)
        
        return entries

    def _create_entry(self, root_path: Path, audio_files: List[str], image_files: List[str]) -> Optional[Dict]:
        """Creates a dictionary entry for a book"""
        relative_path = root_path.relative_to(self.input_dir)
        
        # Skip if files are directly in input directory
        if str(relative_path) == '.':
            self.logger.warning(f"Skipping files in root directory: {audio_files}")
            return None
        
        # Use the first audio file for metadata but keep track of all files
        primary_audio = audio_files[0]
        
        entry = {
            'root_path': str(root_path),
            'relative_path': str(relative_path),
            'audio_files': audio_files,
            'full_audio_path': str(root_path / primary_audio),
            'image_files': image_files,
            'folder_structure': self._get_folder_structure(root_path, audio_files),
            'status': 'pending'
        }
        
        self.logger.debug(f"Created entry: {entry}")
        return entry

    def _get_folder_structure(self, root_path: Path, files: List[str]) -> str:
        """Returns a formatted string showing the folder structure"""
        try:
            # Convert root_path to Path if it's a string
            if isinstance(root_path, str):
                root_path = Path(root_path)
            
            # Get relative path from input directory
            rel_path = Path(root_path).relative_to(self.input_dir)
            
            # Handle root directory case
            if str(rel_path) == '.':
                return '\n'.join(files)
            
            # Create structure string
            structure = [str(rel_path)]
            for file in files:
                structure.append(f"  {file}")
            
            return '\n'.join(structure)
        except Exception as e:
            self.logger.error(f"Error creating folder structure: {str(e)}")
            return str(root_path) 