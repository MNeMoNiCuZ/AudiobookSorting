from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QTableWidget, QTableWidgetItem, 
    QHeaderView, QTextEdit, QLabel, QFrame,
    QStyledItemDelegate
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap, QIcon, QColor
from typing import Dict, Optional, Callable
import logging
from pathlib import Path
import re

class FileStructureDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QTextEdit(parent)
        editor.setReadOnly(True)
        return editor

    def setEditorData(self, editor, index):
        value = index.data(Qt.ItemDataRole.DisplayRole)
        editor.setText(value)

class AudiobookOrganizerGUI(QMainWindow):
    def __init__(self, 
                 on_approve: Callable, 
                 on_reject: Callable, 
                 on_save: Callable,
                 on_llm_query: Callable,
                 on_query_llm_all: Callable,
                 data_manager):
        super().__init__()
        self.on_approve = on_approve
        self.on_reject = on_reject
        self.on_save = on_save
        self.on_llm_query = on_llm_query
        self.on_query_llm_all = on_query_llm_all
        self.data_manager = data_manager
        self.logger = logging.getLogger(__name__)
        
        self.setWindowTitle("Audiobook Organizer")
        self.setMinimumSize(1200, 800)
        self.setup_gui()

    def setup_gui(self):
        # Create central widget and main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Create table
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            'Folder Structure', 'Author', 'Series', 
            'Index', 'Title', 'Source', 'Status', 'Actions'
        ])

        # Set column properties
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)  # Folder Structure
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)      # Author
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)      # Series
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)        # Index
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)      # Title
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)        # Source
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)        # Status
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)        # Actions

        # Set minimum column widths
        self.table.setColumnWidth(0, 300)  # Folder Structure
        self.table.setColumnWidth(3, 60)   # Index
        self.table.setColumnWidth(5, 80)   # Source
        self.table.setColumnWidth(6, 80)   # Status
        self.table.setColumnWidth(7, 100)  # Actions

        # Set the custom delegate for the folder structure column
        delegate = FileStructureDelegate()
        self.table.setItemDelegateForColumn(0, delegate)

        # Add table to layout
        layout.addWidget(self.table)

        # Create bottom button panel
        button_panel = QHBoxLayout()
        
        # Add Query LLM button
        query_llm_button = QPushButton("Query LLM for all")
        query_llm_button.setStyleSheet("""
            QPushButton {
                background-color: #FFD700;
                color: black;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #FFC700;
            }
        """)
        query_llm_button.clicked.connect(self.on_query_llm_all)
        
        # Existing save button
        save_button = QPushButton("Save All")
        save_button.clicked.connect(self.on_save)
        save_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        
        button_panel.addWidget(query_llm_button)
        button_panel.addStretch()
        button_panel.addWidget(save_button)
        layout.addLayout(button_panel)

        # Connect cell editing signal
        self.table.cellChanged.connect(self.cell_changed)

    def create_action_buttons(self, row):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(8)

        # Add LLM button
        llm_btn = QPushButton("LLM")
        llm_btn.setFixedWidth(40)
        llm_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFD700;
                color: black;
                border-radius: 4px;
                padding: 4px;
            }
            QPushButton:hover {
                background-color: #FFC700;
            }
        """)
        llm_btn.clicked.connect(lambda: self.on_llm_query(row))

        # Existing approve/reject buttons
        approve_btn = QPushButton("✓")
        approve_btn.setFixedWidth(30)
        approve_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border-radius: 4px;
                padding: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        approve_btn.clicked.connect(lambda: self.on_approve(row))

        reject_btn = QPushButton("✗")
        reject_btn.setFixedWidth(30)
        reject_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border-radius: 4px;
                padding: 4px;
            }
            QPushButton:hover {
                background-color: #d32f2f;
            }
        """)
        reject_btn.clicked.connect(lambda: self.on_reject(row))

        layout.addWidget(llm_btn)
        layout.addWidget(approve_btn)
        layout.addWidget(reject_btn)
        layout.addStretch()
        
        return widget

    def update_entry(self, entry_id: str, data: Dict):
        """Updates or adds an entry to the table"""
        row = self.find_entry_row(entry_id)
        if row == -1:
            row = self.table.rowCount()
            self.table.insertRow(row)

        # Format series index if it exists and is numeric
        series_index = data.get('series_index', '')
        if series_index and str(series_index).isdigit():
            series_index = f"{int(series_index):02d}"

        # Get list of fields that came from LLM
        llm_fields = data.get('llm_fields', [])

        # Create items for each column
        items = [
            (str(data.get('folder_structure', '')), entry_id, False, False),
            (str(data.get('author', 'Unknown')), None, True, 'author' in llm_fields),
            (str(data.get('series', '')), None, True, 'series' in llm_fields),
            (series_index, None, True, 'series_index' in llm_fields),
            (str(data.get('title', 'Unknown')), None, True, 'title' in llm_fields),
            (str(data.get('source', 'none')), None, False, False),
            (str(data.get('status', 'pending')), None, False, False)
        ]

        for col, (text, user_data, editable, is_llm) in enumerate(items):
            item = QTableWidgetItem(text)
            if user_data:
                item.setData(Qt.ItemDataRole.UserRole, user_data)
            
            # Set flags based on editability
            flags = item.flags()
            if not editable:
                flags &= ~Qt.ItemFlag.ItemIsEditable
            item.setFlags(flags)
            
            # Style LLM-provided fields with bold red text only
            if is_llm:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor('#FF0000'))
            
            self.table.setItem(row, col, item)

        # Set row color based on status only
        self._set_row_color(row, data.get('status', 'pending'), entry_id)

        # Add action buttons
        action_widget = self.create_action_buttons(row)
        self.table.setCellWidget(row, 7, action_widget)
        self.table.resizeRowToContents(row)

    def format_folder_structure(self, structure: str) -> str:
        """Formats the folder structure for better readability"""
        lines = structure.split('\n')
        return '\n'.join(f"{'  ' * line.count('/')}{line.strip()}" for line in lines) 

    def get_entry_id(self, row: int) -> Optional[str]:
        """Gets the entry ID for a given row"""
        if row >= 0 and row < self.table.rowCount():
            item = self.table.item(row, 0)
            if item:
                return item.data(Qt.ItemDataRole.UserRole)
        return None 

    def cell_changed(self, row: int, column: int):
        """Handles cell editing"""
        item = self.table.item(row, column)
        if not item:
            return
        
        entry_id = self.get_entry_id(row)
        if not entry_id:
            return
        
        # Map column index to data key
        column_keys = ['folder_structure', 'author', 'series', 'series_index', 'title', 'source', 'status']
        if column < len(column_keys):
            key = column_keys[column]
            value = item.text()
            
            # Update data manager
            entry = self.data_manager.get_entry(entry_id)
            if entry:
                entry[key] = value
                self.data_manager.update_entry(entry_id, entry) 

    def _set_row_color(self, row: int, status: str, folder_path: str = None):
        """Sets the background color for an entire row"""
        colors = {
            'approved': QColor(232, 245, 233),  # Light green
            'rejected': QColor(255, 220, 220),  # Light red
            'risky': QColor(255, 253, 231),     # Light yellow for LLM and shared folders
            'default': QColor(255, 255, 255)    # White
        }
        
        # Get base color based on status
        color = colors.get(status.lower(), colors['default'])
        
        # Apply color to all columns in the row
        for col in range(self.table.columnCount()):
            # For regular cells
            item = self.table.item(row, col)
            if item:
                item.setBackground(color)
            
            # For the action column widget
            widget = self.table.cellWidget(row, col)
            if widget:
                # Set container background while preserving button styles
                widget.setAutoFillBackground(True)
                palette = widget.palette()
                palette.setColor(widget.backgroundRole(), color)
                widget.setPalette(palette)
                
                # Ensure buttons maintain their styles
                for button in widget.findChildren(QPushButton):
                    button.setAutoFillBackground(True)
                    # Keep original button style
                    current_style = button.styleSheet()
                    if not current_style:
                        continue
                    # Remove any background-color from current style if exists
                    cleaned_style = re.sub(r'background-color:[^;]+;', '', current_style)
                    button.setStyleSheet(cleaned_style)
        
        # Check for shared folder condition if not approved
        if status != 'approved' and folder_path:
            folder = str(Path(folder_path).parent)
            folder_count = 0
            folder_entries = []
            
            # Count entries sharing this folder
            for i in range(self.table.rowCount()):
                other_item = self.table.item(i, 0)
                if other_item:
                    other_path = str(Path(other_item.data(Qt.ItemDataRole.UserRole)).parent)
                    if other_path == folder:
                        folder_count += 1
                        folder_entries.append(i)
            
            # If multiple entries share the folder, color them yellow
            if folder_count > 1:
                for i in folder_entries:
                    for col in range(self.table.columnCount()):
                        item = self.table.item(i, col)
                        if item:
                            item.setBackground(colors['risky'])

    def find_entry_row(self, entry_id: str) -> int:
        """Find the row index for an entry ID"""
        for row in range(self.table.rowCount()):
            if self.get_entry_id(row) == entry_id:
                return row
        return -1 