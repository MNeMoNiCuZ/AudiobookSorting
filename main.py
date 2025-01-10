import sys
from PyQt6.QtWidgets import QApplication
import logging
from pathlib import Path
from scripts import (
    FileScanner,
    MetadataExtractor,
    DataManager,
    AudiobookOrganizerGUI
)

class AudiobookOrganizer:
    def __init__(self, input_dir: str):
        self.input_dir = Path(input_dir)
        self.setup_logging()
        
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
            data_manager=self.data_manager
        )
        self.gui.show()
        
        # Scan directory and process entries
        self.process_entries()

    def setup_logging(self):
        """Sets up logging configuration"""
        logging.basicConfig(
            level=logging.WARNING,
            format='%(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        # Set specific loggers to higher levels
        logging.getLogger('scripts.metadata_extractor').setLevel(logging.ERROR)
        logging.getLogger('scripts.file_scanner').setLevel(logging.WARNING)
        logging.getLogger('scripts.gui').setLevel(logging.WARNING)

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

    def run(self):
        """Starts the application"""
        return self.app.exec()

if __name__ == "__main__":
    app = AudiobookOrganizer("input")
    sys.exit(app.run()) 