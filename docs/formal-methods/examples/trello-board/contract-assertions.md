# Contract Assertion Templates

Generated from formal contracts. Each operation has precondition and postcondition checks.

## Create Board (POST /1/boards)

### Precondition Tests
- [ ] **PRE-BOA-CRE-001**: Verify user is authenticated
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-CRE-002**: Verify name is non-empty string
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-CRE-REQ-name**: Verify name is provided
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-BOA-CRE-001**: Verify board created with status active
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-CRE-002**: Verify board.id returned
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-CRE-003**: Verify user added as admin member
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-CRE-004**: Verify default lists created if defaultLists=true
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 401: not authenticated
- [ ] Status 400: name is empty or missing

## Read Board (GET /1/boards/{id})

### Precondition Tests
- [ ] **PRE-BOA-REA-001**: Verify user is authenticated
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-REA-002**: Verify user has access to board
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-BOA-REA-001**: Verify board object returned with requested fields
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 401: not authenticated
- [ ] Status 404: board does not exist
- [ ] Status 401: user does not have access

## Update Board (PUT /1/boards/{id})

### Precondition Tests
- [ ] **PRE-BOA-UPD-001**: Verify user is authenticated
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-UPD-002**: Verify user has write access to board
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-UPD-003**: Verify board is not deleted
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-BOA-UPD-001**: Verify board updated with new values
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-UPD-002**: Verify dateLastActivity updated
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 401: not authenticated or no write access
- [ ] Status 404: board does not exist

## Delete Board (DELETE /1/boards/{id})

### Precondition Tests
- [ ] **PRE-BOA-DEL-001**: Verify user is authenticated
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-DEL-002**: Verify user is board admin
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-BOA-DEL-001**: Verify board marked as deleted
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-DEL-002**: Verify board no longer appears in member board lists
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-DEL-003**: Verify all lists and cards become inaccessible
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 401: not authenticated or not admin
- [ ] Status 404: board does not exist

## Close Board (PUT /1/boards/{id})

### Precondition Tests
- [ ] **PRE-BOA-CLO-001**: Verify user is authenticated
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-CLO-002**: Verify user has write access
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-CLO-003**: Verify board is active
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-BOA-CLO-001**: Verify board.closed set to true
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-CLO-002**: Verify board moves to closed boards view
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 401: not authenticated or no write access

## Reopen Board (PUT /1/boards/{id})

### Precondition Tests
- [ ] **PRE-BOA-REO-001**: Verify user is authenticated
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-REO-002**: Verify user has write access
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-REO-003**: Verify board is closed
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-BOA-REO-001**: Verify board.closed set to false
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-REO-002**: Verify board reappears in active boards view
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 401: not authenticated or no write access

## List Board (GET /1/members/me/boards)

### Precondition Tests
- [ ] **PRE-BOA-LIS-001**: Verify user is authenticated
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-BOA-LIS-001**: Verify array of board objects returned
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-LIS-002**: Verify only boards user has access to
  - Call correctly → assert postcondition holds

### Error Case Tests
- [ ] Status 401: not authenticated

## Star Board (PUT /1/boards/{id})

### Precondition Tests
- [ ] **PRE-BOA-STA-001**: Verify user is authenticated
  - Call WITHOUT this condition → expect error
- [ ] **PRE-BOA-STA-002**: Verify user is board member
  - Call WITHOUT this condition → expect error

### Postcondition Tests
- [ ] **POST-BOA-STA-001**: Verify board.starred set to true for this user
  - Call correctly → assert postcondition holds
- [ ] **POST-BOA-STA-002**: Verify board appears in starred section
  - Call correctly → assert postcondition holds

### Error Case Tests
