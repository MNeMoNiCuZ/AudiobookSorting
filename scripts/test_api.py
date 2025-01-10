import logging
from scripts.utils import setup_logging
from scripts.api_query import BookAPIClient
from pprint import pprint

def test_api_search():
    setup_logging(debug=True)
    logger = logging.getLogger(__name__)
    api_client = BookAPIClient()

    # Test cases
    test_cases = [
        {
            "name": "Complete data",
            "metadata": {
                "title": "The Icarus Plot",
                "author": "Timothy Zahn",
                "series": "The Icarus Saga",
                "series_index": "1"
            }
        },
        {
            "name": "Title only",
            "metadata": {
                "title": "The Twisted Ones",
                "author": "",
                "series": "",
                "series_index": ""
            }
        },
        {
            "name": "Series book",
            "metadata": {
                "title": "Ghost of the Shadowfort",
                "author": "",
                "series": "Bladeborn",
                "series_index": "2"
            }
        },
        {
            "name": "Author only",
            "metadata": {
                "title": "",
                "author": "T. Kingfisher",
                "series": "",
                "series_index": ""
            }
        }
    ]

    for test in test_cases:
        print("\n" + "="*50)
        print(f"Testing: {test['name']}")
        print("\nInput metadata:")
        pprint(test['metadata'])
        
        try:
            result = api_client.search_book(test['metadata'])
            print("\nAPI Result:")
            pprint(result)
            
            if result:
                # Show which fields were filled
                print("\nFields updated:")
                for field in ['author', 'title', 'series', 'series_index']:
                    if not test['metadata'][field] and result.get(field):
                        print(f"- {field}: {result[field]}")
            else:
                print("\nNo API results found")
                
        except Exception as e:
            print(f"\nError during API search: {str(e)}")

if __name__ == "__main__":
    test_api_search() 