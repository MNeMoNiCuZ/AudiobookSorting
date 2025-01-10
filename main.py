import sys
from PyQt6.QtWidgets import QApplication
import logging
from pathlib import Path
from scripts import (
    FileScanner,
    MetadataExtractor,
    DataManager,
    AudiobookOrganizerGUI,
    LLMQueryClient
)

def setup_logging():
    """Configure logging for the application"""
    # Create logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Create console handler and set level
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)

    # Add console handler to logger
    logger.addHandler(console_handler)
    
    # Set specific loggers to appropriate levels
    logging.getLogger('scripts.metadata_extractor').setLevel(logging.ERROR)
    logging.getLogger('scripts.file_scanner').setLevel(logging.WARNING)
    logging.getLogger('scripts.gui').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)

class AudiobookOrganizer:
    def __init__(self, input_dir: str):
        setup_logging()  # Initialize logging
        self.logger = logging.getLogger(__name__)
        self.input_dir = Path(input_dir)
        
        # Initialize QApplication first
        self.app = QApplication(sys.argv)
        
        # Initialize components
        self.scanner = FileScanner(input_dir)
        self.metadata_extractor = MetadataExtractor()
        self.data_manager = DataManager()
        
        # Setup GUI
        self.gui = AudiobookOrganizerGUI(
            on_approve=self.approve_entry,
            on_reject=self.reject_entry,
            on_save=self.save_entries,
            on_llm_query=self.query_llm_for_entry,
            on_query_llm_all=self.query_llm_all,
            data_manager=self.data_manager
        )
        self.gui.show()
        
        # Scan directory and process entries
        self.process_entries()

    def process_entries(self):
        """Scans directory and processes all entries"""
        entries = self.scanner.scan_directory()
        
        for entry in entries:
            try:
                # Extract metadata
                metadata = self.metadata_extractor.extract_metadata(entry['full_audio_path'])
                
                # Combine entry data
                entry_data = {**entry, **metadata}
                
                # Generate unique ID for entry
                entry_id = str(Path(entry['full_audio_path']).relative_to(self.input_dir))
                
                # Update data manager
                self.data_manager.update_entry(entry_id, entry_data)
                
                # Update GUI
                self.gui.update_entry(entry_id, entry_data)
            except Exception as e:
                self.logger.error(f"Error processing entry {entry.get('full_audio_path', 'unknown')}: {str(e)}")

    def approve_entry(self, row: int):
        """Handles entry approval"""
        entry_id = self.gui.get_entry_id(row)
        if entry_id:
            entry = self.data_manager.get_entry(entry_id)
            entry['status'] = 'approved'
            self.data_manager.update_entry(entry_id, entry)
            self.gui.update_entry(entry_id, entry)

    def reject_entry(self, row: int):
        """Handles entry rejection"""
        entry_id = self.gui.get_entry_id(row)
        if entry_id:
            entry = self.data_manager.get_entry(entry_id)
            entry['status'] = 'rejected'
            self.data_manager.update_entry(entry_id, entry)
            self.gui.update_entry(entry_id, entry)

    def save_entries(self):
        """Saves all entries"""
        self.data_manager.save_entries()
        self.logger.info("Entries saved successfully")

    def query_llm_for_entry(self, row: int):
        """Query LLM for a single entry"""
        entry_id = self.gui.get_entry_id(row)
        if entry_id:
            entry = self.data_manager.get_entry(entry_id)
            if not entry:
                return

            # Set status to risky immediately when querying LLM
            entry['status'] = 'risky'
            self.data_manager.update_entry(entry_id, entry)
            self.gui.update_entry(entry_id, entry)

            # Create file structure from available data
            file_structure = {
                'path': entry.get('relative_path', entry.get('root_path', '')),
                'files': entry.get('audio_files', [])
            }
            
            # Create verified metadata dict
            verified_metadata = {
                'title': entry.get('title', ''),
                'author': entry.get('author', ''),
                'series': entry.get('series', ''),
                'series_index': entry.get('series_index', '')
            }
            
            # Log the query details
            self.logger.info("Sending LLM query for:")
            self.logger.info(f"Path: {file_structure['path']}")
            self.logger.info("Files:")
            for file in file_structure['files']:
                self.logger.info(f"  - {file}")
            self.logger.info("Current metadata:")
            for key, value in verified_metadata.items():
                self.logger.info(f"  {key}: {value}")
            
            # Query LLM
            llm_client = LLMQueryClient()
            result = llm_client.query_metadata(verified_metadata, file_structure)
            
            if result:
                # Log the response
                self.logger.info("LLM Response:")
                for key, value in result.items():
                    self.logger.info(f"  {key}: {value}")
                
                # Only update empty fields
                updated = False
                llm_fields = entry.get('llm_fields', [])
                
                for field in ['title', 'author', 'series', 'series_index']:
                    if not entry.get(field) and result.get(field):
                        entry[field] = result[field]
                        llm_fields.append(field)
                        updated = True
                
                if updated:
                    entry['source'] = 'llm'
                    entry['llm_fields'] = llm_fields
                    entry['status'] = 'risky'  # Always set to risky when LLM updates
                    self.data_manager.update_entry(entry_id, entry)
                    self.gui.update_entry(entry_id, entry)

    def query_llm_all(self):
        """Query LLM for all entries with missing information"""
        entries = self.data_manager.get_all_entries()
        for entry_id, entry in entries.items():
            # Check if any required fields are missing
            if not all([entry.get(f) for f in ['title', 'author', 'series', 'series_index']]):
                # Find row index for this entry
                row = self.gui.find_entry_row(entry_id)
                if row >= 0:
                    # Set status to risky immediately
                    entry['status'] = 'risky'
                    self.data_manager.update_entry(entry_id, entry)
                    self.gui.update_entry(entry_id, entry)
                    
                    # Now perform the LLM query
                    self.query_llm_for_entry(row)

    def run(self):
        """Starts the application"""
        return self.app.exec()

if __name__ == "__main__":
    app = AudiobookOrganizer("input")
    sys.exit(app.run()) 