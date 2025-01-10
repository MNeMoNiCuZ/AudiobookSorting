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
import webbrowser

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
                 on_apply: Callable,
                 on_apply_all: Callable,
                 on_approve_all: Callable,
                 on_reject_all: Callable,
                 data_manager):
        super().__init__()
        self.on_approve = on_approve
        self.on_reject = on_reject
        self.on_save = on_save
        self.on_llm_query = on_llm_query
        self.on_query_llm_all = on_query_llm_all
        self.on_apply = on_apply
        self.on_apply_all = on_apply_all
        self.on_approve_all = on_approve_all
        self.on_reject_all = on_reject_all
        self.data_manager = data_manager
        self.logger = logging.getLogger(__name__)
        
        self.setWindowTitle("Audiobook Organizer")
        self.setMinimumSize(1420, 800)
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
        for col in range(self.table.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)

        # Set default column widths
        self.table.setColumnWidth(0, 300)  # Folder Structure
        self.table.setColumnWidth(1, 200)  # Author
        self.table.setColumnWidth(2, 200)  # Series
        self.table.setColumnWidth(3, 60)   # Index
        self.table.setColumnWidth(4, 200)  # Title
        self.table.setColumnWidth(5, 80)   # Source
        self.table.setColumnWidth(6, 80)   # Status
        self.table.setColumnWidth(7, 240)  # Actions

        # Set the custom delegate for the folder structure column
        delegate = FileStructureDelegate()
        self.table.setItemDelegateForColumn(0, delegate)

        # Add table to layout
        layout.addWidget(self.table)

        # Create bottom button panel
        button_panel = QHBoxLayout()
        
        # Add Query LLM All button
        query_llm_button = QPushButton("Query LLM All")
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
        
        # Add Approve All button
        approve_all_button = QPushButton("Approve All")
        approve_all_button.setStyleSheet("""
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
        approve_all_button.clicked.connect(self.on_approve_all)
        
        # Add Reject All button
        reject_all_button = QPushButton("Reject All")
        reject_all_button.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #d32f2f;
            }
        """)
        reject_all_button.clicked.connect(self.on_reject_all)
        
        # Add Apply All button
        apply_all_button = QPushButton("Apply All")
        apply_all_button.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
        """)
        apply_all_button.clicked.connect(self.on_apply_all)
        
        # Add buttons to panel with centering
        button_panel.addStretch()
        button_panel.addWidget(query_llm_button)
        button_panel.addWidget(approve_all_button)
        button_panel.addWidget(reject_all_button)
        button_panel.addWidget(apply_all_button)
        button_panel.addStretch()
        
        layout.addLayout(button_panel)

        # Connect cell editing signal
        self.table.cellChanged.connect(self.cell_changed)

    def create_action_buttons(self, row):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(8)

        # Add GR button
        gr_btn = QPushButton("GR")
        gr_btn.setFixedWidth(40)
        gr_btn.setStyleSheet("""
            QPushButton {
                background-color: #800080;  /* Purple color */
                color: white;
                border-radius: 4px;
                padding: 4px;
            }
            QPushButton:hover {
                background-color: #9932CC;  /* Lighter purple on hover */
            }
        """)
        gr_btn.clicked.connect(lambda: self.on_gr_query(row))

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

        # Add Apply button
        apply_btn = QPushButton("APPLY")
        apply_btn.setFixedWidth(50)
        apply_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border-radius: 4px;
                padding: 4px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
        """)
        apply_btn.clicked.connect(lambda: self.on_apply(row))

        layout.addWidget(gr_btn)
        layout.addWidget(llm_btn)
        layout.addWidget(approve_btn)
        layout.addWidget(reject_btn)
        layout.addWidget(apply_btn)
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

    def _set_row_color(self, row: int, status: str, entry_id: str):
        """Set the row color based on status"""
        color = {
            'approved': QColor('#E8F5E9'),  # Light green
            'rejected': QColor('#FFEBEE'),  # Light red
            'applied': QColor('#E3F2FD'),   # Light blue
            'risky': QColor('#FFF9C4'),     # Light yellow
            'pending': QColor('white')
        }.get(status, QColor('white'))
        
        for col in range(self.table.columnCount()):
            item = self.table.item(row, col)
            if item:
                item.setBackground(color)

    def find_entry_row(self, entry_id: str) -> int:
        """Find the row index for an entry ID"""
        for row in range(self.table.rowCount()):
            if self.get_entry_id(row) == entry_id:
                return row
        return -1 

    def on_gr_query(self, row):
        entry_id = self.get_entry_id(row)
        if entry_id:
            entry = self.data_manager.get_entry(entry_id)
            if entry:
                # Check if author and title are not from LLM
                llm_fields = entry.get('llm_fields', [])
                if 'author' not in llm_fields and 'title' not in llm_fields:
                    author = entry.get('author', '')
                    title = entry.get('title', '')

                    # Strip numbers and special characters
                    author_clean = re.sub(r'[^a-zA-Z\s]', '', author)
                    title_clean = re.sub(r'[^a-zA-Z\s]', '', title)

                    # Construct the Goodreads search URL
                    query = f"{author_clean} {title_clean}".strip().replace(' ', '+')
                    url = f"https://www.goodreads.com/search?utf8=%E2%9C%93&q={query}&search_type=books&search%5Bfield%5D=on"

                    # Open the URL in the default web browser
                    webbrowser.open_new_tab(url) 