#!/usr/bin/env python
import rospy
from scipy.optimize import minimize
import numpy as np
from sub8_ros_tools import wait_for_param, thread_lock, rosmsg_to_numpy
import threading
from sub8_msgs.msg import Thrust, ThrusterCmd, Alarm
from sub8_msgs.srv import (ThrusterInfo, UpdateThrusterLayout, UpdateThrusterLayoutResponse,
                           BMatrix, BMatrixResponse)
from geometry_msgs.msg import WrenchStamped
lock = threading.Lock()
from sub8_alarm import AlarmListener


class ThrusterMapper(object):
    _min_command_time = rospy.Duration(0.05)
    min_commandable_thrust = 1e-2  # Newtons

    def __init__(self):
        '''The layout should be a dictionary of the form used in thruster_mapper.launch
        an excerpt is quoted here for posterity

        busses:
          - port: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A403IMC9-if00-port0
            thrusters:
              FLV: {
                node_id: 10,
                frame_id: /base_link,
                position: [0.1583, 0.169, 0.0142],
                direction: [0, 0, -1]
              }
              FLL: {
                node_id: 11,
                frame_id: /base_link,
                position: [0.2678, 0.2795, 0],
                direction: [-0.866, 0.5, 0]
              }

          - port: ....
            thrusters: ....

        '''
        self.num_thrusters = 0
        rospy.init_node('thruster_mapper')
        self.last_command_time = rospy.Time.now()
        self.thruster_layout = wait_for_param('busses')
        self.thruster_name_map = []
        self.dropped_thrusters = []
        self.B = self.generate_B(self.thruster_layout)
        self.min_thrusts, self.max_thrusts = self.get_ranges()

        #self.kill_listener = AlarmListener('kill', self.kill_cb)
        # Not the proper way to do this at all, but the kill listener class is being wonky right now
        self.kill_listener = rospy.Subscriber('/alarm_raise', Alarm, self.kill_cb)
        self.killed = True

        self.update_layout_server = rospy.Service('update_thruster_layout', UpdateThrusterLayout, self.update_layout)
        # Expose B matrix through a srv
        self.b_matrix_server = rospy.Service('b_matrix', BMatrix, self.get_b_matrix)

        self.wrench_sub = rospy.Subscriber('wrench', WrenchStamped, self.request_wrench_cb, queue_size=1)
        self.thruster_pub = rospy.Publisher('thrusters/thrust', Thrust, queue_size=1)

    def kill_cb(self, msg):
        if msg.clear is False and msg.alarm_name == "kill":
            self.killed = True
        if msg.clear is True and msg.alarm_name == "kill":
            self.killed = False

    @thread_lock(lock)
    def update_layout(self, srv):
        '''Update the physical thruster layout.
        This should only be done in a thruster-out event
        '''
        rospy.logwarn("Layout in update...")
        self.dropped_thrusters = srv.dropped_thrusters
        for thruster_name in self.dropped_thrusters:
            thruster_index = self.thruster_name_map.index(thruster_name)
            self.min_thrusts[thruster_index] = -self.min_commandable_thrust * 0.5
            self.max_thrusts[thruster_index] = self.min_commandable_thrust * 0.5

        rospy.logwarn("Layout updated")
        return UpdateThrusterLayoutResponse()

    def get_ranges(self):
        '''Get upper and lower thrust limits for each thruster
            --> Add range service proxy using thruster names
                --> This is not necessary, since they are all the same thruster
        '''
        '''
        range_service = 'thrusters/thruster_range'
        rospy.logwarn("Waiting for service {}".format(range_service))
        rospy.wait_for_service(range_service)
        rospy.logwarn("Got {}".format(range_service))

        range_service_proxy = rospy.ServiceProxy(range_service, ThrusterInfo)
        thruster_range = range_service_proxy(0)

        minima = np.array([thruster_range.min_force] * self.num_thrusters)
        maxima = np.array([thruster_range.max_force] * self.num_thrusters)
        '''
        return np.array(([-85.16798401, -85.16798401, -85.16798401, -85.16798401, -85.16798401,
 -85.16798401, -85.16798401, -85.16798401])), np.array(([85.16798401, 85.16798401, 85.16798401, 85.16798401, 85.16798401
, 85.1679840, 85.16798401, 85.16798401]))

    def get_thruster_wrench(self, position, direction):
        '''Compute a single column of B, or the wrench created by a particular thruster'''
        assert np.isclose(1.0, np.linalg.norm(direction), atol=1e-3), "Direction must be a unit vector"
        forces = direction
        torques = np.cross(position, forces)
        wrench_column = np.hstack([forces, torques])
        return np.transpose(wrench_column)

    def get_b_matrix(self, srv):
        ''' Return a copy of the B matrix flattened into a 1-D row-major order list '''
        return BMatrixResponse(self.B.flatten())

    def generate_B(self, layout):
        '''Construct the control-input matrix
        Each column represents the wrench generated by a single thruster

        The single letter "B" is conventionally used to refer to a matrix which converts
         a vector of control inputs to a wrench

        Meaning where u = [thrust_1, ... thrust_n],
         B * u = [Fx, Fy, Fz, Tx, Ty, Tz]
        '''
        # Maintain an ordered list, tracking which column corresponds to which thruster
        self.num_thrusters = 0
        self.thruster_name_map = []
        self.thruster_bounds = []
        B = []
        for port in layout:
            for thruster_name, thruster_info in port['thrusters'].items():
                # Assemble the B matrix by columns
                self.thruster_name_map.append(thruster_name)
                wrench_column = self.get_thruster_wrench(
                    thruster_info['position'],
                    thruster_info['direction']
                )
                self.num_thrusters += 1
                B.append(wrench_column)

        return np.transpose(np.array(B))

    def map(self, wrench):
        '''TODO:
            - Account for variable thrusters
        '''
        thrust_cost = np.diag([1.0] * self.num_thrusters)

        def objective(u):
            '''Compute a cost resembling analytical least squares
            Minimize
                norm((B * u) - wrench) + (u.T * Q * u)
            Subject to
                min_u < u < max_u
            Where
                Q defines the cost of firing the thrusters
                u is a vector where u[n] is the thrust output by thruster_n

            i.e., least squares with bounds
            '''
            error_cost = np.linalg.norm(self.B.dot(u) - wrench) ** 2
            effort_cost = np.transpose(u).dot(thrust_cost).dot(u)
            return error_cost + effort_cost

        def obj_jacobian(u):
            '''Compute the jacobian of the objective function

            [1] Scalar-By-Matrix derivative identities [Online]
                Available: https://en.wikipedia.org/wiki/Matrix_calculus#Scalar-by-vector_identities
            '''
            error_jacobian = 2 * self.B.T.dot(self.B.dot(u) - wrench)
            effort_jacobian = np.transpose(u).dot(2 * thrust_cost)
            return error_jacobian + effort_jacobian

        minimization = minimize(
            method='slsqp',
            fun=objective,
            jac=obj_jacobian,
            x0=(self.min_thrusts + self.max_thrusts) / 2,
            bounds=zip(self.min_thrusts, self.max_thrusts),
            tol=0.1
        )
        return minimization.x, minimization.success

    @thread_lock(lock)
    def request_wrench_cb(self, msg):
        '''Callback for requesting a wrench'''
        time_now = rospy.Time.now()
        if (time_now - self.last_command_time) < self._min_command_time:
            return
        else:
            self.last_command_time = rospy.Time.now()

        force = rosmsg_to_numpy(msg.wrench.force)
        torque = rosmsg_to_numpy(msg.wrench.torque)
        wrench = np.hstack([force, torque])

        success = False
        while not success:
            u, success = self.map(wrench)
            if not success:
                # If we fail to compute, shrink the wrench
                wrench = wrench * 0.75
                continue

            thrust_cmds = []
            # Assemble the list of thrust commands to send
            for name, thrust in zip(self.thruster_name_map, u):
                # > Can speed this up by avoiding appends
                if name in self.dropped_thrusters:
                    continue  # Ignore dropped thrusters

                if np.fabs(thrust) < self.min_commandable_thrust:
                    thrust = 0
                thrust_cmds.append(ThrusterCmd(name=name, thrust=thrust))

        # If the sub is not killed
        if self.killed is False:
            self.thruster_pub.publish(thrust_cmds)

if __name__ == '__main__':
    mapper = ThrusterMapper()
    rospy.spin()
