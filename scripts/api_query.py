import requests
import json
from pathlib import Path
from typing import Dict, Optional, List
import logging
from datetime import datetime
import time
import re

class BookAPIClient:
    def __init__(self, cache_file: str = 'api_cache.json'):
        self.logger = logging.getLogger(__name__)
        self.cache_file = Path(__file__).parent.parent / cache_file
        self.cache = self._load_cache()
        self.last_request_time = 0
        self.min_request_interval = 0.1
        
    def _load_cache(self) -> Dict:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                    # Clean expired entries
                    now = datetime.now().timestamp()
                    cache = {k: v for k, v in cache.items() 
                            if v.get('timestamp', 0) > now - 86400}  # 24h cache
                    return cache
            except Exception as e:
                self.logger.error(f"Error loading cache: {e}")
        return {}

    def _save_cache(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            self.logger.error(f"Error saving cache: {e}")

    def _rate_limit(self):
        """Ensure minimum interval between API requests"""
        now = time.time()
        if now - self.last_request_time < self.min_request_interval:
            time.sleep(self.min_request_interval - (now - self.last_request_time))
        self.last_request_time = time.time()

    def search_book(self, metadata: Dict) -> Optional[Dict]:
        """Search for book information using available metadata"""
        self.logger.info(f"Searching for book with metadata: {metadata}")
        
        # Don't search with insufficient data
        if not metadata.get('title') and not metadata.get('author'):
            self.logger.info("Insufficient metadata for API search")
            return None
        
        cache_key = json.dumps(metadata, sort_keys=True)
        
        if cache_key in self.cache:
            cache_entry = self.cache[cache_key]
            if cache_entry['timestamp'] > datetime.now().timestamp() - 86400:
                return cache_entry['data']

        # Try OpenLibrary first
        result = self._search_openlibrary(metadata)
        if result and self._is_good_match(result, metadata):
            self._save_to_cache(cache_key, result)
            return result

        # Fall back to Google Books if needed
        result = self._search_google_books(metadata)
        if result:
            self._save_to_cache(cache_key, result)
            return result

        return None

    def _search_openlibrary(self, metadata: Dict) -> Optional[Dict]:
        """Search OpenLibrary API"""
        query_parts = []
        
        if metadata.get('title'):
            query_parts.append(f"title:({metadata['title']})")
        if metadata.get('author'):
            query_parts.append(f"author:({metadata['author']})")
        
        if not query_parts:
            return None
            
        try:
            self._rate_limit()
            url = "https://openlibrary.org/search.json"
            params = {
                'q': ' AND '.join(query_parts),
                'fields': 'title,author_name,series,edition_key,key',
                'limit': 10
            }
            
            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if data.get('docs'):
                best_match = self._find_best_openlibrary_match(data['docs'], metadata)
                if best_match:
                    # Get additional details from work endpoint
                    work_key = best_match['key']
                    details = self._get_work_details(work_key)
                    return self._merge_openlibrary_data(best_match, details)
                    
        except Exception as e:
            self.logger.error(f"OpenLibrary API error: {str(e)}")
        
        return None

    def _get_work_details(self, work_key: str) -> Dict:
        """Get detailed work information from OpenLibrary"""
        try:
            self._rate_limit()
            url = f"https://openlibrary.org{work_key}.json"
            response = requests.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error(f"Error getting work details: {str(e)}")
            return {}

    def _merge_openlibrary_data(self, search_result: Dict, work_details: Dict) -> Dict:
        """Merge and normalize OpenLibrary data"""
        series_info = self._extract_series_from_work(work_details)
        
        return {
            'author': search_result.get('author_name', [''])[0],
            'title': search_result.get('title', ''),
            'series': series_info.get('series', ''),
            'series_index': series_info.get('index', ''),
            'source': 'openlibrary'
        }

    def _extract_series_from_work(self, work: Dict) -> Dict:
        """Extract series information from work details"""
        series_info = {'series': '', 'index': ''}
        
        # Check series field
        if 'series' in work:
            series = work['series']
            if isinstance(series, list) and series:
                series_name = series[0].get('title', '')
                series_info['series'] = series_name
                
                # Try to find series index
                for subject in work.get('subjects', []):
                    match = re.search(r'book\s*(\d+)|volume\s*(\d+)', subject.lower())
                    if match:
                        series_info['index'] = match.group(1) or match.group(2)
                        break
        
        return series_info

    def _is_good_match(self, result: Dict, metadata: Dict) -> bool:
        """Verify if the result is a good match for our metadata"""
        # Require exact title match
        if metadata.get('title') and result.get('title'):
            if metadata['title'].lower() != result['title'].lower():
                return False
        
        # Calculate confidence score
        score = 0
        
        # Title match (already verified above)
        if metadata.get('title') and result.get('title'):
            score += 3
            
        # Author match (if we have author data)
        if metadata.get('author') and result.get('author'):
            if metadata['author'].lower() == result['author'].lower():
                score += 2
            
        # Series match
        if metadata.get('series') and result.get('series'):
            if metadata['series'].lower() == result['series'].lower():
                score += 3
            
        # Only accept very high confidence matches
        return score >= 5  # Require multiple field matches

    def _search_google_books(self, metadata: Dict) -> Optional[Dict]:
        """Search Google Books API"""
        # Build search queries in priority order
        search_queries = []
        
        # 1. Most specific search with all available data
        query_parts = []
        if metadata.get('title'):
            query_parts.append(f'intitle:"{metadata["title"]}"')
        if metadata.get('author'):
            query_parts.append(f'inauthor:"{metadata["author"]}"')
        if metadata.get('series'):
            query_parts.append(f'"{metadata["series"]}"')
        if query_parts:
            search_queries.append(' AND '.join(query_parts))

        # 2. Title + Author without quotes
        if metadata.get('title') and metadata.get('author'):
            search_queries.append(f'intitle:{metadata["title"]} inauthor:{metadata["author"]}')

        # 3. Title + Series
        if metadata.get('title') and metadata.get('series'):
            search_queries.append(f'intitle:"{metadata["title"]}" "{metadata["series"]}"')

        # Try each search query until we get a good match
        for query in search_queries:
            self.logger.debug(f"Trying search query: {query}")
            try:
                self._rate_limit()
                url = f"https://www.googleapis.com/books/v1/volumes"
                params = {
                    'q': query,
                    'maxResults': 5  # Get more results to find best match
                }
                
                response = requests.get(url, params=params)
                response.raise_for_status()
                data = response.json()

                if 'items' in data:
                    # Score each result for best match
                    best_match = self._find_best_match(data['items'], metadata)
                    if best_match:
                        self.logger.info(f"Found matching book: {best_match}")
                        return best_match

            except Exception as e:
                self.logger.error(f"Error during API search: {str(e)}")
                continue

        self.logger.info("No matching results found")
        return None

    def _find_best_match(self, items: List[Dict], metadata: Dict) -> Optional[Dict]:
        """Score results to find best match"""
        best_score = 0
        best_match = None

        for item in items:
            score = 0
            vol_info = item.get('volumeInfo', {})
            
            # Title match (exact match scores higher)
            if metadata.get('title'):
                if vol_info.get('title', '').lower() == metadata['title'].lower():
                    score += 3
                elif metadata['title'].lower() in vol_info.get('title', '').lower():
                    score += 1

            # Author match
            if metadata.get('author') and vol_info.get('authors'):
                if metadata['author'].lower() in [a.lower() for a in vol_info['authors']]:
                    score += 2

            # Series match
            if metadata.get('series') and vol_info.get('subtitle'):
                if metadata['series'].lower() in vol_info.get('subtitle', '').lower():
                    score += 2

            if score > best_score:
                best_score = score
                best_match = self._extract_book_data(item)

        # Only return if we have a reasonable match
        return best_match if best_score >= 2 else None

    def _extract_book_data(self, item: Dict) -> Dict:
        """Extract book data from API response"""
        vol_info = item.get('volumeInfo', {})
        return {
            'author': vol_info.get('authors', [''])[0],
            'title': vol_info.get('title', ''),
            'series': self._extract_series_from_google(vol_info),
            'series_index': self._extract_series_index_from_google(vol_info),
            'source': 'api'
        }

    def _save_to_cache(self, cache_key: str, data: Dict):
        """Save data to cache"""
        self.cache[cache_key] = {
            'timestamp': datetime.now().timestamp(),
            'data': data
        }
        self._save_cache()

    def _extract_series_from_google(self, book: Dict) -> str:
        """Extract series information from Google Books response"""
        # Check subtitle for series info
        subtitle = book.get('subtitle', '')
        if subtitle and ('book' in subtitle.lower() or 'volume' in subtitle.lower()):
            return subtitle.split('Book')[0].split('Volume')[0].strip()
        
        # Check categories and title
        categories = book.get('categories', [])
        for category in categories:
            if 'series' in category.lower():
                return category.split('Series')[0].strip()
        
        return ''

    def _extract_series_index_from_google(self, book: Dict) -> str:
        """Extract series index from Google Books response"""
        subtitle = book.get('subtitle', '')
        if subtitle:
            import re
            match = re.search(r'book\s*(\d+)|volume\s*(\d+)', subtitle.lower())
            if match:
                return match.group(1) or match.group(2)
        return ''

    def _extract_series_from_openlibrary(self, book: Dict) -> str:
        """Extract series information from OpenLibrary response"""
        series = book.get('series', [])
        if series and isinstance(series, list) and len(series) > 0:
            return series[0]
        return ''

    def _extract_series_index_from_openlibrary(self, book: Dict) -> str:
        """Extract series index from OpenLibrary response"""
        # OpenLibrary doesn't directly provide series index
        # Try to extract from title or subtitle
        title = book.get('title', '')
        import re
        match = re.search(r'book\s*(\d+)|volume\s*(\d+)', title.lower())
        if match:
            return match.group(1) or match.group(2)
        return '' 