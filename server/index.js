const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const cors = require('cors');

const app = express();
app.use(cors());

const server = http.createServer(app);
const io = new Server(server, {
  cors: {
    origin: '*',
    methods: ['GET', 'POST']
  }
});

const PORT = process.env.PORT || 3000;

// Track users in the room
const users = {};

io.on('connection', (socket) => {
  console.log(`User connected: ${socket.id}`);
  
  socket.on('join-channel', (channelId) => {
    socket.join(channelId);
    if (!users[channelId]) {
      users[channelId] = [];
    }
    users[channelId].push(socket.id);
    console.log(`${socket.id} joined channel ${channelId}`);
    
    // Notify others in the room that a new user joined so they can initiate a peer connection
    socket.to(channelId).emit('user-joined', socket.id);
  });

  socket.on('offer', (payload) => {
    io.to(payload.target).emit('offer', {
      caller: socket.id,
      sdp: payload.sdp
    });
  });

  socket.on('answer', (payload) => {
    io.to(payload.target).emit('answer', {
      caller: socket.id,
      sdp: payload.sdp
    });
  });

  socket.on('ice-candidate', (payload) => {
    io.to(payload.target).emit('ice-candidate', {
      caller: socket.id,
      candidate: payload.candidate
    });
  });

  socket.on('disconnect', () => {
    console.log(`User disconnected: ${socket.id}`);
    for (const channelId in users) {
      if (users[channelId]) {
         users[channelId] = users[channelId].filter(id => id !== socket.id);
         socket.to(channelId).emit('user-disconnected', socket.id);
      }
    }
  });
});

app.get('/', (req, res) => {
  res.send('4G Radio Signaling Server is running.');
});

server.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
