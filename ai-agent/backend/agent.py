from openai import OpenAI
import os
import json

class AIAgent:
    def __init__(self, api_key=None, base_url="https://openrouter.ai/api/v1"):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("API key is required. Set OPENROUTER_API_KEY environment variable.")
        
        self.base_url = base_url
        self.messages = []
        self.reasoning_details = []  # Store reasoning details for continuity
        self.system_prompt = """You are an AI assistant with the ability to execute commands on a virtual machine.
You can help with development tasks, file operations, and system administration.

Available commands (use XML format):
- <tool>read_file{"path": "file.txt"}</tool>: Read contents of a file
- <tool>write_file{"path": "file.txt", "content": "..."}</tool>: Write content to a file
- <tool>run_command{"command": "ls -la"}</tool>: Execute a shell command
- <tool>list_files{"path": "."}</tool>: List files in a directory
- <tool>create_file{"path": "newfile.txt"}</tool>: Create an empty file
- <tool>delete_file{"path": "oldfile.txt"}</tool>: Delete a file
- <tool>search_code{"pattern": "def ", "path": "."}</tool>: Search for code patterns
- <tool>edit_file{"path": "file.txt", "old_str": "...", "new_str": "..."}</tool>: Edit a file

For multiple files to download, use:
- <tool>prepare_download{"files": ["file1.txt", "file2.txt"]}</tool>: Prepare files for zip download

Always think step by step and explain what you're doing.
For code changes, show the diff or explain the changes clearly.
Use skills when appropriate: <skill>skill_name</skill>
"""
        self.messages.append({"role": "system", "content": self.system_prompt})
    
    def add_message(self, role, content, reasoning_details=None):
        message = {"role": role, "content": content}
        if reasoning_details:
            message["reasoning_details"] = reasoning_details
        self.messages.append(message)
    
    def chat(self, user_message, stream=False, use_reasoning=False, model="qwen/qwen-coder-plus:free", api_key_override=None):
        self.add_message("user", user_message)
        
        extra_body = {}
        if use_reasoning:
            extra_body = {"reasoning": {"enabled": True}}
        
        # Use provided API key or get from pool
        current_api_key = api_key_override or self.api_key
        
        try:
            client = OpenAI(
                api_key=current_api_key,
                base_url=self.base_url,
            )
            
            response = client.chat.completions.create(
                model=model,
                messages=self.messages,
                stream=stream,
                extra_body=extra_body
            )
            
            if stream:
                full_response = ""
                reasoning_content = ""
                
                for chunk in response:
                    if chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        yield content
                    
                    # Capture reasoning if available
                    if hasattr(chunk.choices[0].delta, 'reasoning_content') and chunk.choices[0].delta.reasoning_content:
                        reasoning_content += chunk.choices[0].delta.reasoning_content
                
                # Store reasoning details for continuity
                if reasoning_content:
                    self.reasoning_details.append({
                        "role": "assistant",
                        "content": full_response,
                        "reasoning_details": [{"type": "text", "text": reasoning_content}]
                    })
                    self.add_message("assistant", full_response, [{"type": "text", "text": reasoning_content}])
                else:
                    self.add_message("assistant", full_response)
            else:
                assistant_message = response.choices[0].message
                self.add_message("assistant", assistant_message.content, getattr(assistant_message, 'reasoning_details', None))
                return assistant_message.content
                
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            self.add_message("assistant", error_msg)
            if stream:
                yield error_msg
            else:
                return error_msg
    
    def clear_history(self):
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.reasoning_details = []
    
    def get_history(self):
        return self.messages
