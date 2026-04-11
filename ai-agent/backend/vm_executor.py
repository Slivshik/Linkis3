import os
import subprocess
import json
import zipfile
import io
from pathlib import Path
from base64 import b64encode

class VMExecutor:
    def __init__(self, workspace="/workspace"):
        self.workspace = Path(workspace)
        self.allowed_commands = {
            'read_file', 'write_file', 'run_command', 
            'list_files', 'create_file', 'delete_file',
            'search_code', 'edit_file', 'prepare_download'
        }
    
    def execute_command(self, command_type, params):
        """Execute a command on the virtual machine"""
        if command_type not in self.allowed_commands:
            return {"success": False, "error": f"Command '{command_type}' not allowed"}
        
        try:
            method = getattr(self, command_type, None)
            if method:
                return method(**params)
            else:
                return {"success": False, "error": f"Command '{command_type}' not implemented"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def read_file(self, path):
        """Read contents of a file"""
        file_path = self.workspace / path.lstrip('/')
        if not file_path.exists():
            return {"success": False, "error": f"File '{path}' not found"}
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return {"success": True, "content": content, "path": str(file_path)}
    
    def write_file(self, path, content):
        """Write content to a file"""
        file_path = self.workspace / path.lstrip('/')
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return {"success": True, "path": str(file_path), "message": f"File '{path}' written successfully"}
    
    def run_command(self, command):
        """Execute a shell command"""
        # Security: restrict dangerous commands
        dangerous_commands = ['rm -rf', 'sudo', 'su ', 'chmod 777', 'dd if=']
        for dangerous in dangerous_commands:
            if dangerous in command:
                return {"success": False, "error": f"Dangerous command blocked: {dangerous}"}
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=60
            )
            return {
                "success": True,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Command timed out (60s limit)"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def list_files(self, path="."):
        """List files in a directory"""
        dir_path = self.workspace / path.lstrip('/')
        if not dir_path.exists():
            return {"success": False, "error": f"Directory '{path}' not found"}
        
        files = []
        for item in dir_path.iterdir():
            files.append({
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "path": str(item.relative_to(self.workspace))
            })
        
        return {"success": True, "files": files, "directory": str(dir_path)}
    
    def create_file(self, path):
        """Create an empty file"""
        file_path = self.workspace / path.lstrip('/')
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch(exist_ok=True)
        return {"success": True, "path": str(file_path), "message": f"File '{path}' created"}
    
    def delete_file(self, path):
        """Delete a file"""
        file_path = self.workspace / path.lstrip('/')
        if not file_path.exists():
            return {"success": False, "error": f"File '{path}' not found"}
        
        file_path.unlink()
        return {"success": True, "path": str(file_path), "message": f"File '{path}' deleted"}
    
    def search_code(self, pattern, path="."):
        """Search for code patterns using grep"""
        dir_path = self.workspace / path.lstrip('/')
        if not dir_path.exists():
            return {"success": False, "error": f"Directory '{path}' not found"}
        
        try:
            result = subprocess.run(
                ['grep', '-rn', '--include=*.py', '--include=*.js', '--include=*.ts', 
                 '--include=*.html', '--include=*.css', pattern, str(dir_path)],
                capture_output=True,
                text=True,
                timeout=30
            )
            matches = result.stdout.split('\n') if result.stdout else []
            return {"success": True, "matches": matches, "pattern": pattern}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def edit_file(self, path, old_str, new_str):
        """Edit a file by replacing old_str with new_str"""
        file_path = self.workspace / path.lstrip('/')
        if not file_path.exists():
            return {"success": False, "error": f"File '{path}' not found"}
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if old_str not in content:
            return {"success": False, "error": "Old string not found in file"}
        
        new_content = content.replace(old_str, new_str, 1)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        return {"success": True, "path": str(file_path), "message": f"File '{path}' edited successfully"}
    
    def prepare_download(self, files):
        """Prepare multiple files for download as a zip archive"""
        zip_buffer = io.BytesIO()
        missing_files = []
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in files:
                full_path = self.workspace / file_path.lstrip('/')
                if full_path.exists() and full_path.is_file():
                    # Add file to zip with relative path
                    arcname = file_path.lstrip('/')
                    zip_file.write(full_path, arcname)
                else:
                    missing_files.append(file_path)
        
        zip_buffer.seek(0)
        zip_data = zip_buffer.getvalue()
        zip_base64 = b64encode(zip_data).decode('utf-8')
        
        return {
            "success": True,
            "zip_data": zip_base64,
            "filename": "files.zip",
            "file_count": len(files) - len(missing_files),
            "missing_files": missing_files,
            "message": f"Created zip with {len(files) - len(missing_files)} files"
        }
