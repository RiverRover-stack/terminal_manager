#!/usr/bin/env python3
"""
AI Backend Connection Test
Tests Ollama connection and basic functionality
"""

import sys
import os
import requests
import json
from config import Config

def test_ollama_connection():
    """Test Ollama API connection"""
    print("🔍 Testing Ollama connection...")

    try:
        # Test basic connection
        response = requests.get(f"{Config.OLLAMA_URL}/api/tags", timeout=5)
        if response.status_code != 200:
            print(f"❌ Ollama API returned status {response.status_code}")
            return False

        models = response.json().get('models', [])
        model_names = [model['name'] for model in models]

        print(f"✅ Ollama connected successfully")
        print(f"📦 Available models: {', '.join(model_names) if model_names else 'None'}")

        # Test required model
        if Config.OLLAMA_MODEL in model_names:
            print(f"✅ Required model '{Config.OLLAMA_MODEL}' is available")
            return True
        else:
            print(f"⚠️  Required model '{Config.OLLAMA_MODEL}' not found")
            print(f"💡 Install with: ollama pull {Config.OLLAMA_MODEL}")
            return False

    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to Ollama")
        print("💡 Start Ollama with: ollama serve")
        return False
    except Exception as e:
        print(f"❌ Error testing Ollama: {e}")
        return False

def test_llm_generation():
    """Test LLM text generation"""
    print("\n🧠 Testing LLM generation...")

    try:
        payload = {
            "model": Config.OLLAMA_MODEL,
            "prompt": "Say 'Hello from AI Backend!' and nothing else.",
            "stream": False,
            "options": {
                "temperature": 0.1
            }
        }

        response = requests.post(
            f"{Config.OLLAMA_URL}/api/generate",
            json=payload,
            timeout=30
        )

        if response.status_code == 200:
            result = response.json().get('response', '').strip()
            print(f"✅ LLM Response: {result}")
            return True
        else:
            print(f"❌ LLM generation failed with status {response.status_code}")
            return False

    except Exception as e:
        print(f"❌ Error testing LLM generation: {e}")
        return False

def test_directories():
    """Test required directories"""
    print("\n📁 Testing directories...")

    Config.ensure_directories()

    directories = {
        'Dashboard': Config.DASHBOARD_DIR,
        'Data': Config.DATA_DIR,
        'Logs': Config.LOGS_DIR
    }

    all_good = True
    for name, path in directories.items():
        if os.path.exists(path):
            print(f"✅ {name} directory: {path}")
        else:
            print(f"❌ {name} directory missing: {path}")
            all_good = False

    return all_good

def test_dependencies():
    """Test Python dependencies"""
    print("\n📦 Testing Python dependencies...")

    required_packages = [
        'flask', 'pandas', 'requests', 'streamlit',
        'plotly', 'openpyxl', 'flask_cors'
    ]

    missing = []
    for package in required_packages:
        try:
            __import__(package)
            print(f"✅ {package}")
        except ImportError:
            print(f"❌ {package}")
            missing.append(package)

    if missing:
        print(f"\n⚠️  Missing packages: {', '.join(missing)}")
        print("💡 Install with: pip install -r requirements.txt")
        return False

    return True

def main():
    """Run all tests"""
    print("🚀 AI Backend Connection Test\n")

    tests = [
        ("Dependencies", test_dependencies),
        ("Directories", test_directories),
        ("Ollama Connection", test_ollama_connection),
        ("LLM Generation", test_llm_generation)
    ]

    results = []
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"❌ {test_name} test failed with error: {e}")
            results.append((test_name, False))

    # Summary
    print("\n" + "="*50)
    print("📊 Test Results:")

    all_passed = True
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {test_name}: {status}")
        if not passed:
            all_passed = False

    if all_passed:
        print("\n🎉 All tests passed! AI Backend is ready.")
        sys.exit(0)
    else:
        print("\n⚠️  Some tests failed. Check configuration and dependencies.")
        sys.exit(1)

if __name__ == "__main__":
    main()