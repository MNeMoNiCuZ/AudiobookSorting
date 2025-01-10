from mutagen import File
from mutagen.mp4 import MP4
from mutagen.mp3 import MP3
from pathlib import Path
import re
from typing import Dict, Optional, Tuple, List
import logging

# Valid source types for metadata extraction
# These are the only allowed sources for information:
# - metadata: Data extracted from audio file metadata
# - api: Data from external API lookup (not implemented yet)
# - llm: Data from language model analysis (not implemented yet)
# - search_engine: Data from search engine results (not implemented yet)
# - regexp: Data extracted using regular expressions (not implemented yet)
VALID_SOURCES = ['metadata', 'api', 'llm', 'search_engine', 'regexp']

class MetadataExtractor:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.valid_sources = VALID_SOURCES

    def extract_metadata(self, primary_path: str, additional_files: List[str] = None) -> Dict:
        """Extracts metadata from audio files, prioritizing album metadata"""
        primary_path = Path(primary_path)
        metadata = self._create_empty_metadata()
        
        # Extract metadata from primary file
        if primary_path.suffix.lower() == '.m4b':
            metadata = self._extract_m4b_metadata(primary_path)
        else:
            metadata = self._extract_mp3_metadata(primary_path)
        
        # If we have additional files and no album title was found,
        # try to extract from them
        if not metadata.get('album') and additional_files:
            for file_path in additional_files:
                additional_metadata = self.extract_metadata(file_path)
                if additional_metadata.get('album'):
                    metadata['album'] = additional_metadata['album']
                    break
        
        # Use album as title if available
        if metadata.get('album'):
            metadata['title'] = metadata['album']
        
        return metadata

    def _extract_m4b_metadata(self, path: Path) -> Dict:
        """Extracts metadata from M4B file"""
        try:
            audio = MP4(str(path))
            metadata = self._create_empty_metadata()
            
            if audio.tags:
                metadata.update({
                    'author': str(audio.tags.get('\xa9ART', [''])[0] if '\xa9ART' in audio.tags else ''),
                    'title': str(audio.tags.get('\xa9nam', [''])[0] if '\xa9nam' in audio.tags else ''),
                    'album': str(audio.tags.get('\xa9alb', [''])[0] if '\xa9alb' in audio.tags else ''),
                    'source': 'metadata'
                })
                
                # Extract series info
                series_name, series_index = self._extract_series_info(metadata['album'])
                if not series_name:
                    series_name, series_index = self._extract_series_info(metadata['title'])
                
                metadata['series'] = series_name
                metadata['series_index'] = series_index
                
            return metadata
        except Exception as e:
            self.logger.error(f"Error extracting metadata from {path}: {str(e)}")
            return self._create_empty_metadata()

    def _extract_mp3_metadata(self, path: Path) -> Dict:
        """Extracts metadata from MP3 file"""
        audio = MP3(str(path))
        metadata = self._create_empty_metadata()
        
        if audio.tags:
            # First try to get album title
            if 'TALB' in audio.tags:
                metadata['title'] = str(audio.tags['TALB'].text[0])
                metadata['source'] = 'metadata'
            
            # Only use track title if no album title found
            elif 'TIT2' in audio.tags:
                metadata['title'] = str(audio.tags['TIT2'].text[0])
                metadata['source'] = 'metadata'
            
            # Extract artist
            if 'TPE1' in audio.tags:
                metadata['author'] = str(audio.tags['TPE1'].text[0])
            
            # Extract series info from album or title
            series_name, series_index = self._extract_series_info(metadata['title'])
            metadata['series'] = series_name
            metadata['series_index'] = series_index
        
        return metadata

    def _extract_series_info(self, text: str) -> Tuple[str, str]:
        """Attempts to extract series name and index from text"""
        self.logger.info(f"Extracting series info from: {text}")
        
        if not text:
            return '', ''  # Return empty tuple instead of None
        
        # Common patterns for series information
        patterns = [
            r'(.*?)\s*[,-]\s*(?:Book|Volume|Part|#)?\s*(\d+)',  # Series Name - Book 1
            r'(.*?)\s*#(\d+)',  # Series Name #1
            r'(.*?)\s*\((?:Book|Volume|Part)?\s*(\d+)\)',  # Series Name (Book 1)
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                series_name = match.group(1).strip()
                series_index = match.group(2) if match.group(2) else ''  # Ensure index is not None
                return series_name, series_index
                
        return '', ''  # Return empty tuple if no match found

    def _create_empty_metadata(self) -> Dict:
        """Creates an empty metadata dictionary with default values"""
        return {
            'author': '',
            'title': '',
            'album': '',
            'series': '',
            'series_index': '',
            'source': 'none'
        }

    def extract_cover_image(self, file_path: str) -> Optional[bytes]:
        """Extracts cover image from audio file if available"""
        try:
            audio = File(file_path)
            
            if isinstance(audio, MP4):
                for key, value in audio.tags.items():
                    if key.startswith('covr'):
                        return value[0]
                        
            elif isinstance(audio, MP3) and audio.tags:
                for key in audio.tags.keys():
                    if key.startswith('APIC'):
                        return audio.tags[key].data
                        
        except Exception as e:
            self.logger.error(f"Error extracting cover image: {str(e)}")
            
        return None 