import logging
from pprint import pprint
from scripts.llm_query import LLMQueryClient
from scripts.utils import setup_logging
from colorama import init, Fore, Style

# Initialize colorama
init(autoreset=True)

def setup_logging(debug=False):
    # Configure root logger
    logging.basicConfig(level=logging.WARNING)
    
    # Suppress httpx logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    # Configure our app's logger
    logger = logging.getLogger("audiobook_sorting")
    logger.setLevel(logging.INFO if not debug else logging.DEBUG)
    
    # Remove existing handlers
    logger.handlers = []
    
    # Add console handler with minimal formatting
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')  # Simplified format
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger

def test_llm_queries():
    logger = setup_logging(debug=False)
    llm_client = LLMQueryClient()

    test_cases = [
        {
            "name": "Series book with index in filename",
            "verified_metadata": {
                "title": "Ghost of the Shadowfort",
                "author": "",
                "series": "",
                "series_index": "2"
            },
            "file_structure": {
                "path": "/Audiobooks/Fantasy/",
                "files": [
                    "Book 2 - Ghost of the Shadowfort.m4b",
                    "Book 3 - An Echo of Titans.m4b",
                    "Book 4 - The Winds of War The Bladeborn Saga.m4b"
                ]
            }
        },
        {
            "name": "Book with series in title",
            "verified_metadata": {
                "title": "The Winds of War The Bladeborn Saga",
                "author": "",
                "series": "",
                "series_index": ""
            },
            "file_structure": {
                "path": "/Audiobooks/Fantasy/",
                "files": [
                    "Book 2 - Ghost of the Shadowfort.m4b",
                    "Book 3 - An Echo of Titans.m4b",
                    "Book 4 - The Winds of War The Bladeborn Saga.m4b"
                ]
            }
        },
        {
            "name": "Book with only title and author",
            "verified_metadata": {
                "title": "The Twisted Ones",
                "author": "T. Kingfisher",
                "series": "",
                "series_index": ""
            },
            "file_structure": {
                "path": "/Audiobooks/Horror/T. Kingfisher/",
                "files": [
                    "The Twisted Ones.m4b",
                    "What Moves the Dead.m4b",
                    "The Hollow Places.m4b"
                ]
            }
        }
    ]

    for test in test_cases:
        print("\n" + "="*50)
        print(Fore.CYAN + f"Testing: {test['name']}")
        
        print(Fore.GREEN + "\nVerified Metadata:")
        pprint(test['verified_metadata'])
        
        print(Fore.YELLOW + "\nFile Structure:")
        pprint(test['file_structure'])
        
        try:
            result = llm_client.query_metadata(
                test['verified_metadata'],
                test['file_structure']
            )
            
            print(Fore.MAGENTA + "\nLLM Result:")
            if result:
                pprint(result)
                
                print(Fore.GREEN + "\nFields determined:")
                for field in ['author', 'title', 'series', 'series_index']:
                    if result.get(field):
                        print(f"- {field}: {result[field]}")
            else:
                print("No result from LLM")
                
        except Exception as e:
            print(Fore.RED + f"\nError during LLM query: {str(e)}")

if __name__ == "__main__":
    test_llm_queries() 