def process_entries(self):
    """Scans directory and processes all entries"""
    entries = self.scanner.scan_directory()
    
    for entry in entries:
        try:
            # Extract metadata using both primary and additional files
            metadata = self.metadata_extractor.extract_metadata(
                entry['full_audio_path'],
                entry.get('additional_files', [])
            )
            
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