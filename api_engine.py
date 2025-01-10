from configparser import ConfigParser
import openai
import requests
import random

class APIEngine:
    def __init__(self, engine=None):
        """
        Initialize the APIEngine class.

        Args:
            engine (str): Optional. The API engine to use. If not provided, it is read from the config file.
        """
        self.config = self.load_config()
        self.engine = engine if engine else self.config.get('API', 'engine')
        self.api_key = self.get_api_key(self.engine)
        self.default_model = self.get_default_model(self.engine)
        self.allowed_models = self.get_allowed_models(self.engine)
        self.client = None
        self.initialize_api(self.engine, self.default_model)

    def load_config(self):
        """
        Load the configuration from the 'config.ini' file.

        Returns:
            ConfigParser: The configuration object.
        """
        config = ConfigParser()
        config.read('config.ini')
        return config

    def get_api_key(self, engine):
        """
        Retrieve the API key based on the engine type.

        Args:
            engine (str): The API engine.

        Returns:
            str: The API key for the specified engine.

        Raises:
            ValueError: If the engine is unsupported.
        """
        if engine == 'groq':
            return self.config.get('API', 'groq_key')
        elif engine == 'ollama':
            return self.config.get('API', 'ollama_key')
        elif engine == 'openai':
            return self.config.get('API', 'openai_key')
        elif engine == 'sambanova':
            return self.config.get('API', 'sambanova_key')
        elif engine == 'mistral':
            return self.config.get('API', 'mistral_key')
        else:
            raise ValueError(f"Unsupported API engine: {engine}")

    def get_default_model(self, engine):
        """
        Retrieve the default model based on the engine type.

        Args:
            engine (str): The API engine.

        Returns:
            str: The default model for the specified engine.

        Raises:
            ValueError: If the engine is unsupported.
        """
        if engine == 'groq':
            return self.config.get('Models', 'groq_default')
        elif engine == 'ollama':
            return self.config.get('Models', 'ollama_default')
        elif engine == 'openai':
            return self.config.get('Models', 'openai_default')
        elif engine == 'sambanova':
            return self.config.get('Models', 'sambanova_default')
        elif engine == 'mistral':
            return self.config.get('Models', 'mistral_default')
        else:
            raise ValueError(f"Unsupported API engine: {engine}")

    def get_allowed_models(self, engine):
        """
        Retrieve the allowed models based on the engine type.

        Args:
            engine (str): The API engine.

        Returns:
            list: A list of allowed models for the specified engine.

        Raises:
            ValueError: If the engine is unsupported.
        """
        if engine == 'groq':
            return self.config.get('Models', 'groq_allowed').split(', ')
        elif engine == 'ollama':
            return self.config.get('Models', 'ollama_allowed').split(', ')
        elif engine == 'openai':
            return self.config.get('Models', 'openai_allowed').split(', ')
        elif engine == 'sambanova':
            return self.config.get('Models', 'sambanova_allowed').split(', ')
        elif engine == 'mistral':
            return self.config.get('Models', 'mistral_allowed').split(', ')
        else:
            raise ValueError(f"Unsupported API engine: {engine}")

    def initialize_api(self, engine, model):
        """
        Initialize the API client for a specified engine and model.

        Args:
            engine (str): The API engine.
            model (str): The model to be used.
        """
        if engine == 'openai':
            # OpenAI does not require explicit instantiation of OpenAI object
            # It uses `openai` module directly with the API key
            openai.api_key = self.api_key
            self.client = openai  # Store the imported module as the client
        elif engine == 'sambanova':
            # For SambaNova, create an OpenAI client with base_url set to SambaNova's API endpoint
            self.client = openai.OpenAI(
                api_key=self.api_key,
                base_url="https://api.sambanova.ai/v1",
            )
        elif engine == 'mistral':
            from mistralai import Mistral
            self.client = Mistral(api_key=self.api_key)

    def call_api(self, prompt, engine=None, model=None, response_format=None, seed=None):
        """
        Call the API with a given prompt.

        Args:
            prompt (dict): The prompt to send to the API.
            engine (str): Optional. The API engine to use. Defaults to the initialized engine.
            model (str): Optional. The model to use. Defaults to the initialized default model.
            response_format (dict): Optional. The response_format to use for OpenAI.
            seed (int): Optional. A random seed to control randomness in the API output.

        Returns:
            str: The response from the API.

        Raises:
            ValueError: If the engine is unsupported.
        """
        selected_engine = engine if engine else self.engine
        selected_model = model if model else self.default_model

        # Generate a random seed if one is not provided
        if seed is None:
            seed = random.randint(0, 2**32 - 1)

        if selected_engine == 'ollama':
            return self.call_ollama(prompt, selected_model, seed)
        elif selected_engine == 'groq':
            return self.call_groq(prompt, selected_model, seed)
        elif selected_engine == 'openai':
            return self.call_openai(prompt, selected_model, response_format=response_format, seed=seed)
        elif selected_engine == 'sambanova':
            return self.call_sambanova(prompt, selected_model)
        elif selected_engine == 'mistral':
            return self.call_mistral(prompt, selected_model)
        else:
            raise ValueError(f"Unsupported API engine: {selected_engine}")

    def call_ollama(self, prompt, model, seed=None):
        """
        Call the Ollama API with a given prompt and optional seed.

        Args:
            prompt (dict): The prompt to send to the API.
            model (str): The model to use.
            seed (int): Optional. The seed to use for randomness control.

        Returns:
            str: The response from the Ollama API.
        """
        import ollama
        system_message = prompt['messages'][0]['content']
        user_message = prompt['messages'][1]['content']
        combined_message = f"{system_message}\n\n{user_message}"
        response = ollama.generate(
            model=model,
            prompt=combined_message,
            options={'temperature': prompt.get('temperature', 0.5), 'seed': seed}
        )
        return response['response'].strip()

    def call_groq(self, prompt, model, seed=None):
        """
        Call the Groq API with a given prompt and optional seed.

        Args:
            prompt (dict): The prompt to send to the API.
            model (str): The model to use.
            seed (int): Optional. The seed to use for randomness control.

        Returns:
            str: The response from the Groq API.
        """
        from groq import Groq
        client = Groq(api_key=self.api_key)
        response = client.chat.completions.create(
            model=model,
            messages=prompt['messages'],
            temperature=prompt.get('temperature', 0.5),
            max_tokens=prompt.get('max_tokens', 4096),
            top_p=prompt.get('top_p', 0.9),
            stream=False,
            stop=None,
            seed=seed
        )
        return response.choices[0].message.content.strip()

    def call_openai(self, prompt, model, response_format=None, seed=None):
        """
        Call the OpenAI API with a given prompt and optional seed.

        Args:
            prompt (dict): The prompt to send to the API.
            model (str): The model to use.
            response_format (dict): Optional. The response_format to use.
            seed (int): Optional. The seed to use for randomness control.

        Returns:
            dict or str: The response from the OpenAI API.

        Raises:
            ValueError: If the API call fails or response is not as expected.
        """
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": prompt['messages'],
            "temperature": prompt.get('temperature', 0.5),
            "max_tokens": prompt.get('max_tokens', 4096)
        }
        if response_format:
            data["response_format"] = response_format
        if seed is not None:
            data["seed"] = seed
        response = requests.post(url, headers=headers, json=data)

        if response.status_code != 200:
            raise ValueError(f"Failed to call OpenAI API: {response.text}")

        response_json = response.json()
        try:
            choice = response_json['choices'][0]
            message = choice['message']
            if 'parsed' in message:
                return message['parsed']
            elif 'content' in message:
                return message['content'].strip()
            elif 'refusal' in message:
                return message['refusal']
            else:
                raise ValueError("Response does not contain parsed, content, or refusal")
        except (KeyError, IndexError) as e:
            raise ValueError(f"Unexpected response format: {e}")

    def call_sambanova(self, prompt, model):
        """
        Call the SambaNova API with a given prompt.

        Args:
            prompt (dict): The prompt to send to the API.
            model (str): The model to use.

        Returns:
            str: The response from the SambaNova API.
        """
        # Initialize the client if not already done
        if self.client is None:
            self.initialize_api(self.engine, model)

        response = self.client.chat.completions.create(
            model=model,
            messages=prompt['messages'],
            temperature=prompt.get('temperature', 0.5),
            top_p=prompt.get('top_p', 0.1),
            max_tokens=prompt.get('max_tokens', 4096)
        )

        try:
            return response.choices[0].message.content.strip()
        except (AttributeError, IndexError, KeyError) as e:
            raise ValueError(f"Unexpected response format from SambaNova API: {e}")

    def call_mistral(self, prompt, model):
        """
        Call the Mistral API with a given prompt.

        Args:
            prompt (dict): The prompt to send to the API.
            model (str): The model to use.

        Returns:
            str: The response from the Mistral API.
        """
        # Initialize the client if not already done
        if self.client is None:
            self.initialize_api(self.engine, model)

        messages = prompt['messages']
        temperature = prompt.get('temperature', 0.5)
        max_tokens = prompt.get('max_tokens', 4096)

        response = self.client.chat.complete(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )

        try:
            return response.choices[0].message.content.strip()
        except (AttributeError, IndexError, KeyError) as e:
            raise ValueError(f"Unexpected response format from Mistral API: {e}")

    def transcribe_audio(self, file_path, prompt=None, response_format="json", language=None, temperature=0.0):
        """
        Transcribe audio using the Groq API.

        Args:
            file_path (str): The path to the audio file to transcribe.
            prompt (str): Optional. A prompt to guide the transcription.
            response_format (str): The format of the transcription response (default is "json").
            language (str): Optional. The language of the audio.
            temperature (float): The temperature setting for the transcription model.

        Returns:
            str: The transcribed text.
        """
        from groq import Groq
        client = Groq(api_key=self.api_key)
        with open(file_path, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=(file_path, file.read()),
                model="whisper-large-v3",
                prompt=prompt,
                response_format=response_format,
                language=language,
                temperature=temperature
            )
        return transcription.text
