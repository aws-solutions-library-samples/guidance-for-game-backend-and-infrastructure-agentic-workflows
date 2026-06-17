"""
Unit tests for semantic memory utilities.

Tests the semantic memory saving and extraction logic without external dependencies.
"""

# Standard library
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_boto_client():
    """Mock boto3 client for AgentCore Memory."""
    with patch("boto3.client") as mock_client:
        client_instance = Mock()
        mock_client.return_value = client_instance
        yield client_instance


def test_save_semantic_memory_success(mock_boto_client):
    """Test successful semantic memory save."""
    # Local modules
    from utils.semantic_memory import save_semantic_memory

    # Mock memory ID
    with patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"):
        # Mock successful response
        mock_boto_client.batch_create_memory_records.return_value = {
            "successfulRecords": [{"memoryRecordId": "mem-123", "status": "SUCCEEDED"}],
            "failedRecords": [],
        }

        result = save_semantic_memory("test-actor", "Test memory content")

        assert result is True
        mock_boto_client.batch_create_memory_records.assert_called_once()

        # Verify call parameters
        call_args = mock_boto_client.batch_create_memory_records.call_args
        assert call_args[1]["memoryId"] == "test-memory-id"
        assert len(call_args[1]["records"]) == 1
        assert call_args[1]["records"][0]["namespaces"] == ["test-actor"]
        assert call_args[1]["records"][0]["content"]["text"] == "Test memory content"
        assert "timestamp" in call_args[1]["records"][0]


def test_save_semantic_memory_failure(mock_boto_client):
    """Test semantic memory save failure handling."""
    # Local modules
    from utils.semantic_memory import save_semantic_memory

    # Mock failure
    mock_boto_client.batch_create_memory_records.side_effect = Exception("API Error")

    result = save_semantic_memory("test-actor", "Test content")

    assert result is False


def test_save_semantic_memory_no_memory_id():
    """Test semantic memory save when memory ID not configured."""
    # Local modules
    from utils.semantic_memory import save_semantic_memory

    with patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", None):
        result = save_semantic_memory("test-actor", "Test content")
        assert result is False


def test_extract_user_name_simple():
    """Test extracting user name from 'My name is X' pattern."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Hello! My name is John Doe.", "Welcome!")

        mock_save.assert_called_once()
        call_args = mock_save.call_args[0]
        assert call_args[0] == "actor-123"
        assert "John Doe" in call_args[1]


def test_extract_user_name_im_pattern():
    """Test extracting user name from 'I'm X' pattern."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Hi, I'm Alice Smith", "Hello!")

        mock_save.assert_called_once()
        call_args = mock_save.call_args[0]
        assert "Alice Smith" in call_args[1]


def test_extract_user_name_call_me_pattern():
    """Test extracting user name from 'Call me X' pattern."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "You can call me Bob", "Nice to meet you!")

        mock_save.assert_called_once()
        call_args = mock_save.call_args[0]
        assert "Bob" in call_args[1]


def test_extract_user_name_i_am_pattern():
    """Test extracting user name from 'I am X' pattern."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "I am Charlie Brown", "Hello!")

        mock_save.assert_called_once()
        call_args = mock_save.call_args[0]
        assert "Charlie Brown" in call_args[1]


def test_extract_user_name_no_match():
    """Test that no memory is saved when no name pattern is found."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Hello, how are you?", "I am fine!")

        mock_save.assert_not_called()


def test_extract_user_name_case_insensitive():
    """Test that name extraction is case insensitive."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "MY NAME IS DAVID", "Hello!")

        mock_save.assert_called_once()
        call_args = mock_save.call_args[0]
        assert "David" in call_args[1]  # Should be title-cased


def test_extract_user_name_with_special_characters():
    """Test extracting names with hyphens and apostrophes."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "My name is Mary-Jane O'Connor", "Welcome!")

        # Should extract at least the first part
        mock_save.assert_called_once()


# ============================================================================
# Game Pattern Extraction Tests
# ============================================================================


def test_extract_game_type_mmo():
    """Test extracting game type from 'I run an MMO' pattern."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "I'm building an MMO game", "Cool!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Game type: MMO" in call for call in call_args_list)


def test_extract_game_type_battle_royale():
    """Test extracting battle royale game type."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Our battle royale game is growing fast", "Nice!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Game type: BATTLE ROYALE" in call for call in call_args_list)


def test_extract_session_duration():
    """Test extracting session duration."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Our sessions typically last 2 hours", "Got it!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Session duration: 2 hour" in call for call in call_args_list)


def test_extract_concurrent_players():
    """Test extracting concurrent player count with context word."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "We have 500 concurrent players at peak", "Impressive!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Concurrent players: 500" in call for call in call_args_list)


def test_concurrent_players_requires_context():
    """Test that bare '100 players' without context word doesn't match."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "100 players joined the match", "Cool!")
        call_args_list = [str(call) for call in mock_save.call_args_list]
        # Should NOT extract concurrent players without context word
        assert not any("Concurrent players" in call for call in call_args_list)


def test_extract_platform_gamelift():
    """Test extracting GameLift platform choice."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "We use GameLift for our servers", "Great!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Platform: GameLift" in call for call in call_args_list)


def test_extract_platform_agones():
    """Test extracting Agones platform choice."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "We're running on Agones", "Got it!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Platform: Agones" in call for call in call_args_list)


def test_extract_infrastructure_fleet_mapping():
    """Test extracting game-to-fleet infrastructure mapping."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "My MMO runs on fleet-abc-123", "Noted!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Game 'Mmo' runs on fleet: abc-123" in call for call in call_args_list)


def test_extract_infrastructure_cluster_mapping():
    """Test extracting game-to-cluster infrastructure mapping."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Our game uses cluster-gameagent-prod", "Got it!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("runs on cluster: gameagent-prod" in call for call in call_args_list)


def test_extract_peak_players():
    """Test extracting peak player count with time."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "We peak at 10k players on weekends", "Nice!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Peak players: 10k" in call for call in call_args_list)


def test_extract_instance_type():
    """Test extracting instance type."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "We're using c5.large for our servers", "Good choice!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Instance type: c5.large" in call for call in call_args_list)


def test_extract_multiple_patterns():
    """Test extracting multiple patterns from single message."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info(
            "actor-123",
            "My name is John. I run an MMO with 500 concurrent players",
            "Nice!",
        )
        # Should extract at least: name + game type + players
        assert mock_save.call_count >= 3


def test_existing_name_extraction_unchanged():
    """Verify existing name extraction still works after game patterns added."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "My name is Alice", "Hello!")
        mock_save.assert_called()
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Alice" in call for call in call_args_list)


def test_no_game_patterns_matched():
    """Test that no memory is saved when no patterns match."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "What's the weather like today?", "I don't know.")
        mock_save.assert_not_called()


def test_false_positive_name_not_extracted():
    """Test that 'I am running a MMO' doesn't extract 'Running A' as a name."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "I am running a MMO game on Agones", "Cool!")
        # Should extract game type and platform, but NOT a name
        call_args_list = [str(call) for call in mock_save.call_args_list]
        # Verify no name was extracted (no "User's name is" in calls)
        assert not any("User's name is" in call for call in call_args_list)
        # But game type should still be extracted
        assert any("Game type: MMO" in call for call in call_args_list)


def test_false_positive_im_building_not_extracted():
    """Test that 'I'm building a game' doesn't extract 'Building' as a name."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "I'm building a survival game", "Nice!")
        call_args_list = [str(call) for call in mock_save.call_args_list]
        # Verify no name was extracted
        assert not any("User's name is" in call for call in call_args_list)
        # But game type should still be extracted
        assert any("Game type: SURVIVAL" in call for call in call_args_list)


# ============================================================================
# Bug Fix Verification Tests
# ============================================================================


def test_we_are_developing_game_type():
    """Bug #2: Test 'we are developing' extracts game type."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "We are developing an RTS game on EKS", "Cool!")
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Game type: RTS" in call for call in call_args_list)


def test_we_run_game_type():
    """Bug #2: Test 'we run' extracts game type."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "We run a sports game", "Nice!")
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Game type: SPORTS" in call for call in call_args_list)


def test_sessions_that_last():
    """Bug #3: Test 'sessions that last' extracts duration."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Our sessions that last about 45 minutes", "Got it!")
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Session duration: 45 minute" in call for call in call_args_list)


def test_players_play_for_duration():
    """Bug #4: Test 'players play for X minutes' extracts duration."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Players usually play for about 30 minutes", "Nice!")
        call_args_list = [str(call) for call in mock_save.call_args_list]
        assert any("Session duration: 30 minute" in call for call in call_args_list)


def test_off_peak_formatter():
    """Bug #1: Test off-peak time is formatted correctly (not as 'Peak players')."""
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    with patch("utils.semantic_memory.save_semantic_memory") as mock_save:
        extract_and_save_user_info("actor-123", "Off-peak is overnight for us", "Got it!")
        call_args_list = [str(call) for call in mock_save.call_args_list]
        # Should be "Off-peak time: overnight", NOT "Peak players: overnight"
        assert any("Off-peak time: overnight" in call for call in call_args_list)
        assert not any("Peak players: overnight" in call for call in call_args_list)


# ============================================================================
# Deduplication Tests
# ============================================================================


@pytest.fixture
def clear_dedup_cache():
    """Clear the deduplication cache before and after tests."""
    # Local modules
    from utils import semantic_memory

    # Clear before test
    semantic_memory._saved_memory_hashes.clear()
    yield
    # Clear after test
    semantic_memory._saved_memory_hashes.clear()


@pytest.fixture
def mock_memory_client(clear_dedup_cache):
    """Mock the memory client and clear dedup cache for dedup tests."""
    # Local modules
    from utils import semantic_memory

    # Reset the cached client to ensure our mock is used
    semantic_memory._memory_client = None

    with patch("utils.semantic_memory._get_memory_client") as mock_getter:
        client_mock = Mock()
        mock_getter.return_value = client_mock
        client_mock.batch_create_memory_records.return_value = {
            "successfulRecords": [{"memoryRecordId": "mem-123", "status": "SUCCEEDED"}],
            "failedRecords": [],
        }
        yield client_mock


def test_duplicate_memory_skipped(mock_memory_client):
    """Test that duplicate content for same actor is skipped."""
    # Local modules
    from utils.semantic_memory import save_semantic_memory

    with patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"):
        # First save should succeed and call API
        result1 = save_semantic_memory("actor-123", "Test memory content")
        assert result1 is True
        assert mock_memory_client.batch_create_memory_records.call_count == 1

        # Second save with same content should return True but NOT call API
        result2 = save_semantic_memory("actor-123", "Test memory content")
        assert result2 is True
        assert mock_memory_client.batch_create_memory_records.call_count == 1  # Still 1


def test_duplicate_detection_case_insensitive(mock_memory_client):
    """Test that deduplication is case insensitive."""
    # Local modules
    from utils.semantic_memory import save_semantic_memory

    with patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"):
        # First save
        save_semantic_memory("actor-123", "User's name is John")
        assert mock_memory_client.batch_create_memory_records.call_count == 1

        # Same content different case - should be detected as duplicate
        save_semantic_memory("actor-123", "USER'S NAME IS JOHN")
        assert mock_memory_client.batch_create_memory_records.call_count == 1  # Still 1


def test_duplicate_detection_trims_whitespace(mock_memory_client):
    """Test that deduplication trims whitespace."""
    # Local modules
    from utils.semantic_memory import save_semantic_memory

    with patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"):
        # First save
        save_semantic_memory("actor-123", "Game type: MMO")
        assert mock_memory_client.batch_create_memory_records.call_count == 1

        # Same content with extra whitespace - should be detected as duplicate
        save_semantic_memory("actor-123", "  Game type: MMO  ")
        assert mock_memory_client.batch_create_memory_records.call_count == 1  # Still 1


def test_same_content_different_actor_not_duplicate(mock_memory_client):
    """Test that same content for different actors is NOT a duplicate."""
    # Local modules
    from utils.semantic_memory import save_semantic_memory

    with patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"):
        # First actor
        save_semantic_memory("actor-123", "Platform: GameLift")
        assert mock_memory_client.batch_create_memory_records.call_count == 1

        # Different actor with same content - should NOT be duplicate
        save_semantic_memory("actor-456", "Platform: GameLift")
        assert mock_memory_client.batch_create_memory_records.call_count == 2


def test_different_content_same_actor_not_duplicate(mock_memory_client):
    """Test that different content for same actor is NOT a duplicate."""
    # Local modules
    from utils.semantic_memory import save_semantic_memory

    with patch("utils.semantic_memory.BEDROCK_AGENTCORE_MEMORY_ID", "test-memory-id"):
        save_semantic_memory("actor-123", "Platform: GameLift")
        save_semantic_memory("actor-123", "Game type: MMO")
        assert mock_memory_client.batch_create_memory_records.call_count == 2


def test_dedup_helper_functions(clear_dedup_cache):
    """Test the deduplication helper functions directly."""
    # Local modules
    from utils.semantic_memory import _get_content_hash, _is_duplicate_memory, _mark_memory_saved

    # Test hash generation is consistent
    hash1 = _get_content_hash("actor-1", "test content")
    hash2 = _get_content_hash("actor-1", "test content")
    assert hash1 == hash2

    # Test hash is different for different actors
    hash3 = _get_content_hash("actor-2", "test content")
    assert hash1 != hash3

    # Test duplicate detection
    assert _is_duplicate_memory("actor-1", "test content") is False
    _mark_memory_saved("actor-1", "test content")
    assert _is_duplicate_memory("actor-1", "test content") is True
    assert _is_duplicate_memory("actor-2", "test content") is False  # Different actor
