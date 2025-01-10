import json
from pathlib import Path
from typing import Dict, List
import logging

class DataManager:
    def __init__(self, save_file: str = 'book_entries.json'):
        # Get the project root directory (one level up from scripts)
        project_root = Path(__file__).parent.parent
        self.save_file = project_root / save_file
        self.entries: Dict[str, Dict] = {}
        self.logger = logging.getLogger(__name__)
        self.load_entries()

    def load_entries(self) -> None:
        """Loads entries from the save file"""
        if self.save_file.exists():
            try:
                with open(self.save_file, 'r', encoding='utf-8') as f:
                    self.entries = json.load(f)
            except Exception as e:
                self.logger.error(f"Error loading entries: {str(e)}")
                self.entries = {}

    def save_entries(self) -> None:
        """Saves entries to the save file"""
        try:
            # Ensure directory exists
            self.save_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Try to save with write permissions
            with open(self.save_file, 'w', encoding='utf-8') as f:
                json.dump(self.entries, f, indent=2, ensure_ascii=False)
        except PermissionError:
            self.logger.error(f"Permission denied when saving to {self.save_file}")
            # Try alternative location in user's home directory
            alt_path = Path.home() / 'audiobook_organizer_entries.json'
            try:
                with open(alt_path, 'w', encoding='utf-8') as f:
                    json.dump(self.entries, f, indent=2, ensure_ascii=False)
                self.logger.info(f"Saved entries to alternative location: {alt_path}")
                self.save_file = alt_path
            except Exception as e:
                self.logger.error(f"Failed to save to alternative location: {str(e)}")
        except Exception as e:
            self.logger.error(f"Error saving entries: {str(e)}")

    def update_entry(self, entry_id: str, entry_data: Dict) -> None:
        """Updates a single entry and saves to file"""
        self.entries[entry_id] = entry_data
        self.save_entries()  # Save after each update

    def get_entry(self, entry_id: str) -> Dict:
        """Retrieves a single entry"""
        return self.entries.get(entry_id, {})

    def get_all_entries(self) -> Dict:
        """Returns all entries"""
        return self.entries

    def set_entry_status(self, entry_id: str, status: str) -> None:
        """Sets the status of an entry (approved/rejected)"""
        if entry_id in self.entries:
            self.entries[entry_id]['status'] = status 